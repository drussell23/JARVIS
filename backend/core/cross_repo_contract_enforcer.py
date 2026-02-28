"""
Cross-repository contract enforcement for boot and runtime drift detection.

This module provides deterministic compatibility checks across JARVIS, Prime,
and Reactor by enforcing:
1. Health schema contracts
2. Protocol version windows
3. Handshake compatibility and required capabilities
4. Hysteresis-based runtime drift transitions
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import aiohttp

from backend.core.handshake_protocol import (
    HandshakeManager,
    HandshakeProposal,
    HandshakeResponse,
    evaluate_handshake,
)
from backend.core.protocol_version_gate import (
    ProtocolVersion,
    extract_version_from_health,
)
from backend.core.startup_contracts import validate_health_response

logger = logging.getLogger(__name__)

# WorkspaceAction semantic contract constants.
WORKSPACE_ACTION_CONTRACT_NAME = "workspace_action"
WORKSPACE_ACTION_CONTRACT_VERSION_V1 = "workspace_action.v1"


@dataclass(frozen=True)
class ContractTarget:
    """Contract requirements for one remote component."""

    name: str
    endpoint: str
    health_schema_key: str
    min_api_version: str
    max_api_version: str
    required_capabilities: Tuple[str, ...] = ()
    required: bool = True
    require_handshake: bool = True
    allow_legacy_handshake: bool = True
    workspace_action_contract_required: bool = False
    workspace_action_contract_version: str = WORKSPACE_ACTION_CONTRACT_VERSION_V1
    workspace_action_schema_hash: str = ""


@dataclass(frozen=True)
class ContractCheckResult:
    """Outcome of one target contract check."""

    target: ContractTarget
    ok: bool
    reason: str
    api_version: Optional[str] = None
    schema_violations: Tuple[str, ...] = ()
    semantic_contract_violations: Tuple[str, ...] = ()
    capabilities: Tuple[str, ...] = ()
    handshake_mode: str = "none"  # native|legacy|none
    checked_at: float = 0.0


@dataclass(frozen=True)
class DriftTransition:
    """State transition emitted by ContractDriftMonitor."""

    target: str
    from_state: str
    to_state: str
    reason: str
    checked_at: float = 0.0


@dataclass
class _DriftState:
    state: str = "unknown"  # unknown|healthy|degraded
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_reason: str = ""
    updated_at: float = 0.0


class ContractDriftMonitor:
    """
    Hysteresis tracker for contract drift state transitions.

    A target transitions to degraded only after N consecutive failures and
    recovers only after M consecutive successes.
    """

    def __init__(self, failure_threshold: int = 2, recovery_threshold: int = 2):
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_threshold = max(1, int(recovery_threshold))
        self._state: Dict[str, _DriftState] = {}

    def update(self, results: Dict[str, ContractCheckResult]) -> List[DriftTransition]:
        now = time.time()
        transitions: List[DriftTransition] = []

        for name, result in results.items():
            state = self._state.setdefault(name, _DriftState(updated_at=now))

            if result.ok:
                state.consecutive_successes += 1
                state.consecutive_failures = 0
                state.last_reason = ""
                if state.state != "healthy" and state.consecutive_successes >= self.recovery_threshold:
                    old = state.state
                    state.state = "healthy"
                    transitions.append(
                        DriftTransition(
                            target=name,
                            from_state=old,
                            to_state="healthy",
                            reason="contract_restored",
                            checked_at=now,
                        )
                    )
            else:
                state.consecutive_failures += 1
                state.consecutive_successes = 0
                state.last_reason = result.reason
                if state.state != "degraded" and state.consecutive_failures >= self.failure_threshold:
                    old = state.state
                    state.state = "degraded"
                    transitions.append(
                        DriftTransition(
                            target=name,
                            from_state=old,
                            to_state="degraded",
                            reason=result.reason,
                            checked_at=now,
                        )
                    )

            state.updated_at = now

        return transitions

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {
            name: {
                "state": s.state,
                "consecutive_failures": s.consecutive_failures,
                "consecutive_successes": s.consecutive_successes,
                "last_reason": s.last_reason,
                "updated_at": s.updated_at,
            }
            for name, s in self._state.items()
        }


class CrossRepoContractEnforcer:
    """Performs strict contract checks for cross-repo components."""

    def __init__(
        self,
        supervisor_instance_id: str,
        local_protocol_version: str = "1.0.0",
        request_timeout_s: float = 8.0,
    ) -> None:
        self.supervisor_instance_id = supervisor_instance_id
        self.local_protocol_version = local_protocol_version
        self.request_timeout_s = max(1.0, float(request_timeout_s))
        self._handshake_mgr = HandshakeManager()
        self._workspace_action_schema_hash_cache: Optional[str] = None

    async def check_many(self, targets: Sequence[ContractTarget]) -> Dict[str, ContractCheckResult]:
        timeout = aiohttp.ClientTimeout(total=self.request_timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [self._check_target(session, t) for t in targets]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        mapped: Dict[str, ContractCheckResult] = {}
        now = time.time()
        for target, result in zip(targets, results):
            if isinstance(result, Exception):
                mapped[target.name] = ContractCheckResult(
                    target=target,
                    ok=False,
                    reason=f"contract_check_exception:{type(result).__name__}",
                    checked_at=now,
                )
            else:
                mapped[target.name] = result
        return mapped

    async def _check_target(
        self,
        session: aiohttp.ClientSession,
        target: ContractTarget,
    ) -> ContractCheckResult:
        checked_at = time.time()
        health_ok, health_data, health_reason = await self._fetch_health(session, target.endpoint)
        if not health_ok:
            return ContractCheckResult(
                target=target,
                ok=False,
                reason=health_reason or "health_unreachable",
                checked_at=checked_at,
            )

        schema_violations = tuple(validate_health_response(target.health_schema_key, health_data))
        if schema_violations:
            return ContractCheckResult(
                target=target,
                ok=False,
                reason=f"schema_violation:{'; '.join(schema_violations[:2])}",
                schema_violations=schema_violations,
                checked_at=checked_at,
            )

        health_api_version = extract_version_from_health(health_data)

        handshake_ok = True
        handshake_reason = "handshake_not_required"
        handshake_mode = "none"
        capabilities: Tuple[str, ...] = ()
        handshake_metadata: Dict[str, Any] = {}

        if target.require_handshake:
            (
                handshake_ok,
                handshake_reason,
                handshake_mode,
                capabilities,
                handshake_api_version,
                handshake_metadata,
            ) = await self._perform_handshake(session, target, health_data)
        else:
            handshake_api_version = None

        if not handshake_ok:
            return ContractCheckResult(
                target=target,
                ok=False,
                reason=handshake_reason,
                api_version=health_api_version or handshake_api_version,
                capabilities=capabilities,
                handshake_mode=handshake_mode,
                checked_at=checked_at,
            )

        api_version = health_api_version or handshake_api_version
        if not api_version:
            return ContractCheckResult(
                target=target,
                ok=False,
                reason="missing_api_version",
                capabilities=capabilities,
                handshake_mode=handshake_mode,
                checked_at=checked_at,
            )

        version_ok, version_reason = self._check_version_window(
            remote_api_version=api_version,
            min_api_version=target.min_api_version,
            max_api_version=target.max_api_version,
        )
        if not version_ok:
            return ContractCheckResult(
                target=target,
                ok=False,
                reason=f"version_incompatible:{version_reason}",
                api_version=api_version,
                capabilities=capabilities,
                handshake_mode=handshake_mode,
                checked_at=checked_at,
            )

        semantic_contract_violations = tuple(
            self._validate_semantic_contracts(
                target=target,
                health_data=health_data,
                handshake_metadata=handshake_metadata,
            )
        )
        if semantic_contract_violations:
            return ContractCheckResult(
                target=target,
                ok=False,
                reason=f"semantic_contract_violation:{'; '.join(semantic_contract_violations[:2])}",
                api_version=api_version,
                capabilities=capabilities,
                handshake_mode=handshake_mode,
                semantic_contract_violations=semantic_contract_violations,
                checked_at=checked_at,
            )

        return ContractCheckResult(
            target=target,
            ok=True,
            reason="contract_ok",
            api_version=api_version,
            capabilities=capabilities,
            handshake_mode=handshake_mode,
            checked_at=checked_at,
        )

    async def _fetch_health(
        self,
        session: aiohttp.ClientSession,
        endpoint: str,
    ) -> Tuple[bool, Dict[str, Any], str]:
        url = f"{endpoint.rstrip('/')}/health"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False, {}, f"health_http_{resp.status}"
                payload = await resp.json()
                if not isinstance(payload, dict):
                    return False, {}, "health_not_object"
                return True, payload, ""
        except asyncio.TimeoutError:
            return False, {}, "health_timeout"
        except Exception as e:
            return False, {}, f"health_error:{type(e).__name__}"

    def _check_version_window(
        self,
        *,
        remote_api_version: str,
        min_api_version: str,
        max_api_version: str,
    ) -> Tuple[bool, str]:
        try:
            local = ProtocolVersion.parse(
                self.local_protocol_version,
                min_compat=min_api_version,
                max_compat=max_api_version,
            )
            remote = ProtocolVersion.parse(remote_api_version)
            return local.is_compatible_with(remote)
        except Exception as e:
            return False, f"version_parse_error:{e}"

    async def _perform_handshake(
        self,
        session: aiohttp.ClientSession,
        target: ContractTarget,
        health_data: Dict[str, Any],
    ) -> Tuple[bool, str, str, Tuple[str, ...], Optional[str], Dict[str, Any]]:
        proposal = HandshakeProposal(
            supervisor_epoch=int(time.time()),
            supervisor_instance_id=self.supervisor_instance_id,
            expected_api_version_min=target.min_api_version,
            expected_api_version_max=target.max_api_version,
            required_capabilities=tuple(target.required_capabilities),
            health_schema_hash=self._schema_hash(target.health_schema_key),
            heartbeat_interval_s=5.0,
            heartbeat_ttl_s=30.0,
            protocol_version=self.local_protocol_version,
        )

        url = f"{target.endpoint.rstrip('/')}/lifecycle/handshake"
        try:
            async with session.post(url, json=proposal.to_dict()) as resp:
                if resp.status == 404:
                    if not target.allow_legacy_handshake:
                        return False, "handshake_endpoint_missing", "none", (), None, {}

                    legacy = await self._handshake_mgr.synthesize_legacy_response(
                        target.name,
                        health_data,
                    )
                    ok, reason = evaluate_handshake(proposal, legacy)
                    caps = tuple(legacy.capabilities)
                    if not ok:
                        return (
                            False,
                            f"legacy_handshake_failed:{reason}",
                            "legacy",
                            caps,
                            legacy.api_version,
                            {},
                        )
                    return True, "legacy_handshake_ok", "legacy", caps, legacy.api_version, {}

                if resp.status >= 400:
                    return False, f"handshake_http_{resp.status}", "none", (), None, {}

                data = await resp.json()
                response = HandshakeResponse.from_dict(data if isinstance(data, dict) else {})
                response_metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
                if not isinstance(response_metadata, dict):
                    response_metadata = {}
                ok, reason = evaluate_handshake(proposal, response)
                caps = tuple(response.capabilities)
                if not ok:
                    return (
                        False,
                        f"handshake_incompatible:{reason}",
                        "native",
                        caps,
                        response.api_version,
                        response_metadata,
                    )

                # Legacy api version under native handshake can still be rejected
                # when legacy handshake fallback is disabled.
                if (
                    not target.allow_legacy_handshake
                    and (response.api_version == "0.0.0")
                ):
                    return (
                        False,
                        "legacy_api_version_not_allowed",
                        "native",
                        caps,
                        response.api_version,
                        response_metadata,
                    )

                return (
                    True,
                    "native_handshake_ok",
                    "native",
                    caps,
                    response.api_version,
                    response_metadata,
                )

        except asyncio.TimeoutError:
            return False, "handshake_timeout", "none", (), None, {}
        except Exception as e:
            return False, f"handshake_error:{type(e).__name__}", "none", (), None, {}

    def _workspace_action_schema_hash(self) -> str:
        """Canonical schema hash for WorkspaceAction v1 semantic contract."""
        if self._workspace_action_schema_hash_cache:
            return self._workspace_action_schema_hash_cache

        canonical_schema = {
            "schema_version": WORKSPACE_ACTION_CONTRACT_VERSION_V1,
            "required_top_level": [
                "schema_version",
                "request_id",
                "correlation_id",
                "domain",
                "query",
                "plan",
            ],
            "plan_required": ["plan_id", "nodes", "on_failure", "max_parallelism"],
            "node_required": [
                "node_id",
                "action",
                "args",
                "depends_on",
                "can_parallelize",
                "timeout_ms",
                "side_effect",
                "requires_confirmation",
                "idempotency",
                "output_key",
            ],
            "idempotency_required": ["scope", "key"],
            "domain": "workspace",
        }
        serialized = json.dumps(canonical_schema, sort_keys=True, separators=(",", ":"))
        self._workspace_action_schema_hash_cache = hashlib.sha256(
            serialized.encode("utf-8")
        ).hexdigest()
        return self._workspace_action_schema_hash_cache

    def _extract_semantic_contracts(
        self,
        *,
        health_data: Dict[str, Any],
        handshake_metadata: Dict[str, Any],
    ) -> Dict[str, Dict[str, str]]:
        """
        Extract semantic contracts from handshake metadata and health payload.

        Accepts either:
        - metadata/health: {"semantic_contracts": {"workspace_action": {...}}}
        - metadata/health flat keys:
          workspace_action_contract_version, workspace_action_schema_hash
        """
        extracted: Dict[str, Dict[str, str]] = {}

        def _merge(source: Dict[str, Any]) -> None:
            if not isinstance(source, dict):
                return
            contracts = source.get("semantic_contracts")
            if isinstance(contracts, dict):
                for name, spec in contracts.items():
                    if isinstance(spec, dict):
                        version = str(spec.get("version", "")).strip()
                        schema_hash = str(spec.get("schema_hash", "")).strip()
                        if version or schema_hash:
                            extracted[str(name).strip().lower()] = {
                                "version": version,
                                "schema_hash": schema_hash,
                            }

            # Flat WorkspaceAction keys (compat path).
            wa_version = str(source.get("workspace_action_contract_version", "")).strip()
            wa_hash = str(source.get("workspace_action_schema_hash", "")).strip()
            if wa_version or wa_hash:
                extracted[WORKSPACE_ACTION_CONTRACT_NAME] = {
                    "version": wa_version,
                    "schema_hash": wa_hash,
                }

        _merge(health_data)
        _merge(handshake_metadata)
        return extracted

    def _validate_semantic_contracts(
        self,
        *,
        target: ContractTarget,
        health_data: Dict[str, Any],
        handshake_metadata: Dict[str, Any],
    ) -> List[str]:
        """Validate semantic contracts (WorkspaceAction v1) at boot/runtime."""
        violations: List[str] = []
        contracts = self._extract_semantic_contracts(
            health_data=health_data,
            handshake_metadata=handshake_metadata,
        )

        if not target.workspace_action_contract_required:
            return violations

        workspace_spec = contracts.get(WORKSPACE_ACTION_CONTRACT_NAME)
        if not workspace_spec:
            violations.append("workspace_action:missing")
            return violations

        expected_version = (target.workspace_action_contract_version or "").strip()
        actual_version = (workspace_spec.get("version") or "").strip()
        if expected_version and actual_version != expected_version:
            violations.append(
                f"workspace_action:version_mismatch(expected={expected_version},actual={actual_version or 'missing'})"
            )

        expected_hash = (target.workspace_action_schema_hash or "").strip()
        if not expected_hash and expected_version == WORKSPACE_ACTION_CONTRACT_VERSION_V1:
            expected_hash = self._workspace_action_schema_hash()
        actual_hash = (workspace_spec.get("schema_hash") or "").strip()

        if expected_hash and not actual_hash:
            violations.append("workspace_action:schema_hash_missing")
        elif expected_hash and actual_hash != expected_hash:
            violations.append(
                f"workspace_action:schema_hash_mismatch(expected={expected_hash[:12]},actual={actual_hash[:12]})"
            )

        return violations

    def _schema_hash(self, schema_key: str) -> str:
        payload = json.dumps({"schema_key": schema_key}, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

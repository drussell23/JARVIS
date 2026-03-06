"""Scenario S3: Contract Version Mismatch -> Graceful Degradation.

Simulates a contract version incompatibility between JARVIS and the prime
backend.  When the contract checker detects that the prime handshake
contract is incompatible (version window violation), the system degrades
gracefully by routing to CLOUD_CLAUDE until the contract is restored.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from typing import Any, Dict

from tests.harness.types import (
    ComponentStatus,
    ContractReasonCode,
    ContractStatus,
)


class S03ContractVersionMismatch:
    """Contract version mismatch triggers graceful degradation to cloud."""

    name = "s03_contract_version_mismatch"

    phase_deadlines: Dict[str, float] = {
        "setup": 5.0,
        "inject": 10.0,
        "verify": 10.0,
        "recover": 30.0,
    }

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    # ------------------------------------------------------------------
    # Phases -- each receives (oracle, injector, trace_root_id) from the
    # HarnessOrchestrator.
    # ------------------------------------------------------------------

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Set prime READY, routing LOCAL_PRIME, epoch 1, contract compatible."""
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(1)
        oracle.set_contract_status(
            "prime_handshake",
            ContractStatus(
                compatible=True,
                reason_code=ContractReasonCode.OK,
            ),
        )

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Inject contract version mismatch and degrade."""
        # 1. Set contract to incompatible with version window violation
        oracle.set_contract_status(
            "prime_handshake",
            ContractStatus(
                compatible=False,
                reason_code=ContractReasonCode.VERSION_WINDOW,
                detail="v2.1 vs v3.0",
            ),
        )

        # 2. Emit contract_incompatible event
        oracle.emit_event(
            source="contract_checker",
            event_type="contract_incompatible",
            component="prime",
            old_value="compatible",
            new_value="incompatible",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={"reason": "version_window", "detail": "v2.1 vs v3.0"},
        )

        # 3. Prime degrades and routing switches to cloud
        oracle.set_component_status("prime", ComponentStatus.DEGRADED)
        oracle.set_routing_decision("CLOUD_CLAUDE")

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Confirm routing switched and contract is incompatible."""
        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "CLOUD_CLAUDE",
            deadline=5.0,
            description="routing == CLOUD_CLAUDE",
        )

        # Assert contract status
        contract = oracle.contract_status("prime_handshake")
        assert not contract.compatible, "Contract should be incompatible"
        assert contract.reason_code == ContractReasonCode.VERSION_WINDOW, (
            f"Expected VERSION_WINDOW, got {contract.reason_code}"
        )

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Restore contract compatibility and recover."""
        # 1. Restore contract
        oracle.set_contract_status(
            "prime_handshake",
            ContractStatus(
                compatible=True,
                reason_code=ContractReasonCode.OK,
            ),
        )

        # 2. Emit contract_compatible event
        oracle.emit_event(
            source="contract_checker",
            event_type="contract_compatible",
            component="prime",
            old_value="incompatible",
            new_value="compatible",
            trace_root_id=trace_root_id,
            trace_id="",
        )

        # 3. Restore prime and routing
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")

        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "LOCAL_PRIME",
            deadline=5.0,
            description="routing == LOCAL_PRIME",
        )

# tests/unit/core/test_startup_phase_gate_v298.py
"""v298.0: BOOT_CONTRACT_VALIDATION phase gate tests."""
import pytest
from backend.core.startup_phase_gate import (
    PhaseGateCoordinator,
    StartupPhase,
    GateStatus,
    GateFailureReason,
)


def test_boot_contract_validation_phase_exists():
    """BOOT_CONTRACT_VALIDATION enum member must exist."""
    assert hasattr(StartupPhase, "BOOT_CONTRACT_VALIDATION")


def test_core_ready_depends_on_boot_contract_validation():
    """CORE_READY must list BOOT_CONTRACT_VALIDATION as a dependency."""
    phase = StartupPhase.BOOT_CONTRACT_VALIDATION
    assert phase in StartupPhase.CORE_READY.dependencies


def test_boot_contract_validation_depends_on_core_services():
    """BOOT_CONTRACT_VALIDATION must depend on CORE_SERVICES."""
    assert StartupPhase.CORE_SERVICES in StartupPhase.BOOT_CONTRACT_VALIDATION.dependencies


def test_core_ready_cannot_pass_without_boot_contract_validation():
    """PhaseGateCoordinator must block CORE_READY if BOOT_CONTRACT_VALIDATION is pending."""
    coord = PhaseGateCoordinator()
    # Resolve prereqs except BOOT_CONTRACT_VALIDATION
    coord.resolve(StartupPhase.PREWARM_GCP)
    coord.resolve(StartupPhase.CORE_SERVICES)
    # Attempt CORE_READY without BOOT_CONTRACT_VALIDATION
    result = coord.resolve(StartupPhase.CORE_READY)
    assert result.status == GateStatus.FAILED
    assert result.failure_reason == GateFailureReason.DEPENDENCY_UNMET


def test_core_ready_passes_after_full_chain():
    """Full chain: PREWARM_GCP → CORE_SERVICES → BOOT_CONTRACT_VALIDATION → CORE_READY."""
    coord = PhaseGateCoordinator()
    coord.resolve(StartupPhase.PREWARM_GCP)
    coord.resolve(StartupPhase.CORE_SERVICES)
    coord.resolve(StartupPhase.BOOT_CONTRACT_VALIDATION)
    result = coord.resolve(StartupPhase.CORE_READY)
    assert result.status == GateStatus.PASSED

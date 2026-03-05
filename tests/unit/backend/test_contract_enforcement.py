#!/usr/bin/env python3
"""
Contract enforcement tests for Disease 4: Advisory Contracts = No Contracts.

Run: python3 -m pytest tests/unit/backend/test_contract_enforcement.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from backend.core.startup_contracts import (
    ContractSeverity, ViolationReasonCode, EnvContract, ENV_CONTRACTS,
    ContractViolationRecord, StartupContractViolation,
)


class TestContractSeverityEnum:
    def test_all_severity_levels_exist(self):
        assert ContractSeverity.PRECHECK_BLOCKER == "precheck_blocker"
        assert ContractSeverity.BOOT_BLOCKER == "boot_blocker"
        assert ContractSeverity.BLOCK_BEFORE_READY == "block_before_ready"
        assert ContractSeverity.DEGRADED_ALLOWED == "degraded_allowed"
        assert ContractSeverity.ADVISORY == "advisory"

    def test_exactly_five_levels(self):
        assert len(ContractSeverity) == 5


class TestViolationReasonCodeEnum:
    @pytest.mark.parametrize("code", [
        "malformed_url", "port_conflict", "port_out_of_range",
        "missing_secret", "capability_missing", "schema_incompatible",
        "version_incompatible", "hash_drift_detected", "handshake_failed",
        "health_unreachable", "alias_conflict", "pattern_mismatch",
        "default_fallback_used",
    ])
    def test_reason_code_exists(self, code):
        assert ViolationReasonCode(code) == code


class TestEnvContractSeverity:
    def test_all_contracts_have_severity(self):
        for contract in ENV_CONTRACTS:
            assert hasattr(contract, "severity"), f"{contract.canonical_name} missing severity"
            assert isinstance(contract.severity, ContractSeverity), f"{contract.canonical_name}.severity wrong type"

    def test_port_contracts_are_precheck_blocker(self):
        port_names = {"JARVIS_BACKEND_PORT", "JARVIS_FRONTEND_PORT", "JARVIS_LOADING_SERVER_PORT"}
        for contract in ENV_CONTRACTS:
            if contract.canonical_name in port_names:
                assert contract.severity == ContractSeverity.PRECHECK_BLOCKER

    def test_url_contract_is_precheck_blocker(self):
        for contract in ENV_CONTRACTS:
            if contract.canonical_name == "JARVIS_PRIME_URL":
                assert contract.severity == ContractSeverity.PRECHECK_BLOCKER

    def test_advisory_contracts_exist(self):
        advisory = [c for c in ENV_CONTRACTS if c.severity == ContractSeverity.ADVISORY]
        assert len(advisory) >= 3


class TestContractViolationRecord:
    def test_record_fields(self):
        record = ContractViolationRecord(
            contract_name="JARVIS_PRIME_URL",
            base_severity=ContractSeverity.PRECHECK_BLOCKER,
            effective_severity=ContractSeverity.PRECHECK_BLOCKER,
            reason_code=ViolationReasonCode.MALFORMED_URL,
            violation="URL is malformed",
            value_origin="explicit",
            checked_at_monotonic=1000.0,
            checked_at_utc="2026-03-05T12:00:00Z",
            phase="precheck",
        )
        assert record.contract_name == "JARVIS_PRIME_URL"
        assert record.reason_code == ViolationReasonCode.MALFORMED_URL
        assert record.value_origin == "explicit"

    def test_record_is_frozen(self):
        record = ContractViolationRecord(
            contract_name="test", base_severity=ContractSeverity.ADVISORY,
            effective_severity=ContractSeverity.ADVISORY,
            reason_code=ViolationReasonCode.PATTERN_MISMATCH,
            violation="test", value_origin="default",
            checked_at_monotonic=0.0, checked_at_utc="", phase="precheck",
        )
        with pytest.raises(AttributeError):
            record.contract_name = "changed"


class TestStartupContractViolation:
    def test_exception_carries_violations(self):
        record = ContractViolationRecord(
            contract_name="JARVIS_BACKEND_PORT",
            base_severity=ContractSeverity.PRECHECK_BLOCKER,
            effective_severity=ContractSeverity.PRECHECK_BLOCKER,
            reason_code=ViolationReasonCode.PORT_OUT_OF_RANGE,
            violation="Port 99999 out of range",
            value_origin="explicit",
            checked_at_monotonic=0.0, checked_at_utc="", phase="precheck",
        )
        exc = StartupContractViolation([record])
        assert len(exc.violations) == 1
        assert "JARVIS_BACKEND_PORT" in str(exc)
        assert "port_out_of_range" in str(exc)

    def test_exception_is_exception(self):
        assert issubclass(StartupContractViolation, Exception)

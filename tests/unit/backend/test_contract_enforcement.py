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
    EnvResolution, get_canonical_env, validate_contracts_at_boot,
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


class TestContractStateAuthority:
    """Central violation state authority with dedup."""

    def _make_record(self, name="TEST", severity=None, reason=None, phase="precheck"):
        from backend.core.startup_contracts import ContractSeverity, ViolationReasonCode, ContractViolationRecord
        sev = severity or ContractSeverity.ADVISORY
        rc = reason or ViolationReasonCode.PATTERN_MISMATCH
        return ContractViolationRecord(
            contract_name=name, base_severity=sev, effective_severity=sev,
            reason_code=rc, violation=f"{name} violation", value_origin="explicit",
            checked_at_monotonic=0.0, checked_at_utc="2026-03-05T00:00:00Z", phase=phase,
        )

    def test_record_and_retrieve(self):
        from backend.core.startup_contracts import ContractStateAuthority
        auth = ContractStateAuthority()
        auth.record(self._make_record())
        assert len(auth.get_violations()) == 1

    def test_dedup_same_contract_and_reason(self):
        from backend.core.startup_contracts import ContractStateAuthority, ViolationReasonCode
        auth = ContractStateAuthority()
        auth.record(self._make_record("A", reason=ViolationReasonCode.PATTERN_MISMATCH))
        auth.record(self._make_record("A", reason=ViolationReasonCode.PATTERN_MISMATCH))
        auth.record(self._make_record("A", reason=ViolationReasonCode.PATTERN_MISMATCH))
        assert len(auth.get_violations()) == 1

    def test_different_reasons_not_deduped(self):
        from backend.core.startup_contracts import ContractStateAuthority, ViolationReasonCode
        auth = ContractStateAuthority()
        auth.record(self._make_record("A", reason=ViolationReasonCode.PATTERN_MISMATCH))
        auth.record(self._make_record("A", reason=ViolationReasonCode.ALIAS_CONFLICT))
        assert len(auth.get_violations()) == 2

    def test_has_blockers(self):
        from backend.core.startup_contracts import ContractStateAuthority, ContractSeverity
        auth = ContractStateAuthority()
        auth.record(self._make_record(severity=ContractSeverity.ADVISORY))
        assert not auth.has_blockers()
        auth.record(self._make_record("PORT", severity=ContractSeverity.PRECHECK_BLOCKER))
        assert auth.has_blockers()

    def test_blocking_reasons(self):
        from backend.core.startup_contracts import ContractStateAuthority, ContractSeverity, ViolationReasonCode
        auth = ContractStateAuthority()
        auth.record(self._make_record("PORT", severity=ContractSeverity.PRECHECK_BLOCKER,
                                       reason=ViolationReasonCode.PORT_CONFLICT))
        reasons = auth.blocking_reasons()
        assert "port_conflict" in reasons

    def test_severity_filter(self):
        from backend.core.startup_contracts import ContractStateAuthority, ContractSeverity
        auth = ContractStateAuthority()
        auth.record(self._make_record("A", severity=ContractSeverity.ADVISORY))
        auth.record(self._make_record("B", severity=ContractSeverity.PRECHECK_BLOCKER))
        advisory = auth.get_violations(severity_filter=ContractSeverity.ADVISORY)
        assert len(advisory) == 1
        assert advisory[0].contract_name == "A"

    def test_health_summary_bounded(self):
        from backend.core.startup_contracts import ContractStateAuthority, ContractSeverity, ViolationReasonCode
        auth = ContractStateAuthority()
        for i in range(20):
            auth.record(self._make_record(
                f"C{i}", severity=ContractSeverity.PRECHECK_BLOCKER,
                reason=ViolationReasonCode.PORT_CONFLICT
            ))
        summary = auth.health_summary(max_detail=5)
        assert summary["total_violations"] == 20
        assert len(summary["top_blockers"]) <= 5

    def test_full_report_includes_all(self):
        from backend.core.startup_contracts import ContractStateAuthority
        auth = ContractStateAuthority()
        for i in range(10):
            auth.record(self._make_record(f"C{i}"))
        report = auth.full_report()
        assert len(report["violations"]) == 10


class TestEnvResolution:
    """Default-origin tracing for env var resolution."""

    def test_resolution_fields(self):
        r = EnvResolution(value="8010", origin="explicit", canonical_name="JARVIS_BACKEND_PORT")
        assert r.value == "8010"
        assert r.origin == "explicit"
        assert r.canonical_name == "JARVIS_BACKEND_PORT"

    def test_explicit_origin(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"JARVIS_BACKEND_PORT": "8010"}, clear=False):
            result = get_canonical_env("JARVIS_BACKEND_PORT")
            assert isinstance(result, EnvResolution)
            assert result.value == "8010"
            assert result.origin == "explicit"
            assert result.canonical_name == "JARVIS_BACKEND_PORT"

    def test_alias_origin(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"BACKEND_PORT": "9090"}, clear=True):
            result = get_canonical_env("JARVIS_BACKEND_PORT")
            assert isinstance(result, EnvResolution)
            assert result.value == "9090"
            assert result.origin == "alias:BACKEND_PORT"
            assert result.canonical_name == "JARVIS_BACKEND_PORT"

    def test_default_origin(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            result = get_canonical_env("JARVIS_BACKEND_PORT")
            assert isinstance(result, EnvResolution)
            assert result.value == "8010"
            assert result.origin == "default"
            assert result.canonical_name == "JARVIS_BACKEND_PORT"

    def test_canonical_takes_precedence_over_alias(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"JARVIS_BACKEND_PORT": "8010", "BACKEND_PORT": "9999"}, clear=True):
            result = get_canonical_env("JARVIS_BACKEND_PORT")
            assert isinstance(result, EnvResolution)
            assert result.value == "8010"
            assert result.origin == "explicit"

    def test_unset_no_default_returns_none(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            result = get_canonical_env("JARVIS_PRIME_URL")
            assert result is None

    def test_non_contracted_var_explicit(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"MY_CUSTOM_VAR": "hello"}, clear=False):
            result = get_canonical_env("MY_CUSTOM_VAR")
            assert isinstance(result, EnvResolution)
            assert result.value == "hello"
            assert result.origin == "explicit"

    def test_non_contracted_var_unset(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            result = get_canonical_env("TOTALLY_UNKNOWN_VAR")
            assert result is None

    def test_resolution_is_frozen(self):
        r = EnvResolution(value="x", origin="explicit", canonical_name="Y")
        with pytest.raises(AttributeError):
            r.value = "changed"


class TestSeverityAwareValidation:
    """validate_contracts_at_boot must return structured results."""

    def test_pattern_violation_returns_record(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"JARVIS_BACKEND_PORT": "not_a_number"}, clear=False):
            result = validate_contracts_at_boot()
            assert len(result) > 0
            assert isinstance(result[0], ContractViolationRecord)
            assert result[0].effective_severity == ContractSeverity.PRECHECK_BLOCKER

    def test_port_out_of_range_reason(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"JARVIS_BACKEND_PORT": "99999"}, clear=False):
            result = validate_contracts_at_boot()
            port_violations = [r for r in result if r.contract_name == "JARVIS_BACKEND_PORT"]
            assert len(port_violations) >= 1
            assert port_violations[0].reason_code == ViolationReasonCode.PORT_OUT_OF_RANGE

    def test_clean_env_no_violations(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            result = validate_contracts_at_boot()
            assert len(result) == 0

    def test_alias_conflict_detected(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {
            "JARVIS_BACKEND_PORT": "8010",
            "BACKEND_PORT": "9090",
        }, clear=True):
            result = validate_contracts_at_boot()
            alias_violations = [r for r in result
                                if r.reason_code == ViolationReasonCode.ALIAS_CONFLICT]
            assert len(alias_violations) >= 1

    def test_port_collision_detected(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {
            "JARVIS_BACKEND_PORT": "8010",
            "JARVIS_FRONTEND_PORT": "8010",
        }, clear=True):
            result = validate_contracts_at_boot()
            collisions = [r for r in result
                          if r.reason_code == ViolationReasonCode.PORT_CONFLICT]
            assert len(collisions) >= 1
            assert collisions[0].effective_severity == ContractSeverity.PRECHECK_BLOCKER

    def test_malformed_url_reason(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"JARVIS_PRIME_URL": "not-a-url"}, clear=True):
            result = validate_contracts_at_boot()
            url_violations = [r for r in result if r.contract_name == "JARVIS_PRIME_URL"]
            assert len(url_violations) >= 1
            assert url_violations[0].reason_code == ViolationReasonCode.MALFORMED_URL

    def test_origin_traced_in_violations(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"JARVIS_PRIME_URL": "not-a-url"}, clear=True):
            result = validate_contracts_at_boot()
            url_violations = [r for r in result if r.contract_name == "JARVIS_PRIME_URL"]
            assert len(url_violations) >= 1
            assert url_violations[0].value_origin == "explicit"


class TestPrecheckGateWiring:
    """Preflight gate must be wired in unified_supervisor.py."""

    def test_supervisor_imports_startup_contract_violation(self):
        import ast
        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "startup_contracts" in node.module:
                    names = [alias.name for alias in node.names]
                    if "StartupContractViolation" in names:
                        found = True
        assert found, "unified_supervisor.py must import StartupContractViolation"

    def test_supervisor_references_precheck_blocker(self):
        with open("unified_supervisor.py", "r") as f:
            source = f.read()
        assert "StartupContractViolation" in source
        assert "PRECHECK_BLOCKER" in source

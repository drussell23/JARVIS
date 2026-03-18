"""tests/governance/test_deploy_gate.py — P3-2 safe deploy strategy tests."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.canary_controller import CanaryController, CanaryState
from backend.core.ouroboros.governance.deploy_gate import (
    DeployContract,
    DeployGate,
    PreflightCheck,
    ContractPreflightResult,
)


def _contract(
    service="jarvis",
    from_v="2.2.0",
    to_v="2.3.0",
    rollback_ref="abc1234",
    slice_prefix="",
) -> DeployContract:
    return DeployContract(
        service=service,
        from_version=from_v,
        to_version=to_v,
        rollback_ref=rollback_ref,
        domain_slice_prefix=slice_prefix,
    )


class TestDeployContractPreflight:
    def test_pass_with_valid_contract(self):
        gate = DeployGate()
        result = gate.run_preflight(_contract())
        assert result.passed is True
        assert result.failed_checks == ()

    def test_fail_when_rollback_ref_missing(self):
        gate = DeployGate()
        result = gate.run_preflight(_contract(rollback_ref=""))
        assert result.passed is False
        assert any("rollback_ref" in c for c in result.failed_checks)

    def test_fail_when_versions_empty(self):
        gate = DeployGate()
        result = gate.run_preflight(_contract(from_v="", to_v=""))
        assert result.passed is False
        assert any("version_strings" in c for c in result.failed_checks)

    def test_warn_when_from_eq_to_version(self):
        gate = DeployGate()
        result = gate.run_preflight(_contract(from_v="2.3.0", to_v="2.3.0"))
        # Same version is a warning, not a failure
        assert result.passed is True
        assert any("from_version == to_version" in w for w in result.warnings)

    def test_extra_check_failure_captured(self):
        gate = DeployGate()
        check = PreflightCheck(name="custom", check_fn=lambda: (False, "db_not_ready"))
        result = gate.run_preflight(_contract(), extra_checks=[check])
        assert result.passed is False
        assert any("custom" in c and "db_not_ready" in c for c in result.failed_checks)

    def test_extra_check_warning_advisory(self):
        gate = DeployGate()
        check = PreflightCheck(name="advisory", check_fn=lambda: (True, "slow_replica"))
        result = gate.run_preflight(_contract(), extra_checks=[check])
        assert result.passed is True
        assert any("advisory" in w for w in result.warnings)

    def test_extra_check_exception_captured(self):
        def bad_check():
            raise RuntimeError("network unreachable")
        gate = DeployGate()
        check = PreflightCheck(name="net", check_fn=bad_check)
        result = gate.run_preflight(_contract(), extra_checks=[check])
        assert result.passed is False
        assert any("net" in c and "exception" in c for c in result.failed_checks)


class TestDeployGateSLOIntegration:
    def test_slo_unhealthy_blocks_preflight(self):
        from backend.core.slo_budget import SLOTarget, SLOMetric, SLOHealthModel
        model = SLOHealthModel("svc", [
            SLOTarget(SLOMetric.ERROR_RATE, threshold=0.05, budget_fraction=0.01)
        ])
        for _ in range(10):
            model.record(SLOMetric.ERROR_RATE, 0.99)   # all violations → UNHEALTHY

        gate = DeployGate(slo_model=model)
        result = gate.run_preflight(_contract())
        assert result.passed is False
        assert any("slo_unhealthy" in c for c in result.failed_checks)

    def test_slo_degraded_is_warning_only(self):
        from backend.core.slo_budget import SLOTarget, SLOMetric, SLOHealthModel
        model = SLOHealthModel("svc", [
            SLOTarget(
                SLOMetric.ERROR_RATE,
                threshold=0.05,
                budget_fraction=0.10,
                degraded_burn_multiplier=0.5,  # degraded threshold = 0.05
            )
        ])
        for _ in range(9):
            model.record(SLOMetric.ERROR_RATE, 0.02)
        model.record(SLOMetric.ERROR_RATE, 0.10)   # 1/10 = 10% > 5% → DEGRADED

        gate = DeployGate(slo_model=model)
        result = gate.run_preflight(_contract())
        assert result.passed is True
        assert any("slo_degraded" in w for w in result.warnings)


class TestDeployGateCanaryEvaluation:
    def _gate_with_slice(self, prefix: str) -> tuple[DeployGate, CanaryController]:
        cc = CanaryController()
        cc.register_slice(prefix)
        return DeployGate(canary_controller=cc), cc

    def test_is_go_without_canary_controller(self):
        gate = DeployGate()
        assert gate.is_go_for_deploy(_contract()) is True

    def test_is_go_when_canary_not_promoted(self):
        prefix = "backend/"
        gate, cc = self._gate_with_slice(prefix)
        contract = _contract(slice_prefix=prefix)
        # Slice has no operations → not promoted → NO-GO
        assert gate.is_go_for_deploy(contract) is False

    def test_evaluate_canary_returns_promotion_result(self):
        gate = DeployGate()
        result = gate.evaluate_canary("some/prefix")
        assert result.promoted is True   # no controller → synthetic pass

    def test_trigger_rollback_suspends_slice(self):
        prefix = "backend/"
        gate, cc = self._gate_with_slice(prefix)
        s = cc.get_slice(prefix)
        assert s.state == CanaryState.PENDING
        gate.trigger_rollback(_contract(rollback_ref="old_sha", slice_prefix=prefix), "p95 breached")
        assert s.state == CanaryState.SUSPENDED

    def test_trigger_rollback_no_canary_controller_does_not_raise(self):
        gate = DeployGate()
        gate.trigger_rollback(_contract(), "test")   # should not raise


class TestDeployContractImmutability:
    def test_contract_is_frozen(self):
        import dataclasses
        c = _contract()
        assert dataclasses.is_dataclass(c)
        with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            c.service = "tampered"  # type: ignore[misc]

    def test_result_is_frozen(self):
        import dataclasses
        r = ContractPreflightResult(passed=True, failed_checks=(), warnings=())
        assert dataclasses.is_dataclass(r)
        with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            r.passed = False  # type: ignore[misc]

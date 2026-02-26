from backend.core.gcp_oom_prevention_bridge import (
    DegradationTier,
    MemoryCheckResult,
    MemoryDecision,
    _derive_startup_mode_contract,
    _select_fail_closed_startup_mode,
)


def test_contract_mapping_cloud_required_prefers_cloud_first():
    mode, force, skip = _derive_startup_mode_contract(
        MemoryDecision.CLOUD_REQUIRED,
        DegradationTier.TIER_1_GCP_CLOUD,
        can_proceed_locally=False,
        available_gb=3.0,
    )
    assert (mode, force, skip) == ("cloud_first", False, True)


def test_contract_mapping_degraded_sequential_forces_local_mode():
    mode, force, skip = _derive_startup_mode_contract(
        MemoryDecision.DEGRADED,
        DegradationTier.TIER_3_SEQUENTIAL_LOAD,
        can_proceed_locally=True,
        available_gb=2.8,
    )
    assert (mode, force, skip) == ("sequential", True, True)


def test_contract_mapping_abort_uses_fail_closed_mode():
    mode, force, skip = _derive_startup_mode_contract(
        MemoryDecision.ABORT,
        DegradationTier.TIER_5_ABORT,
        can_proceed_locally=False,
        available_gb=1.2,
    )
    assert (mode, force, skip) == ("minimal", True, True)


def test_fail_closed_mode_defaults_to_sequential_when_memory_unknown():
    assert _select_fail_closed_startup_mode(None) == "sequential"


def test_result_dict_includes_supervisor_contract_fields():
    result = MemoryCheckResult(
        decision=MemoryDecision.DEGRADED,
        can_proceed_locally=True,
        gcp_vm_required=False,
        gcp_vm_ready=False,
        gcp_vm_ip=None,
        available_ram_gb=2.6,
        required_ram_gb=3.0,
        memory_pressure_percent=88.0,
        reason="test",
        recommended_startup_mode="sequential",
        force_mode_apply=True,
        skip_local_prewarm=True,
        bridge_available=True,
    )
    as_dict = result.to_dict()
    assert as_dict["recommended_startup_mode"] == "sequential"
    assert as_dict["force_mode_apply"] is True
    assert as_dict["skip_local_prewarm"] is True
    assert as_dict["bridge_available"] is True

from __future__ import annotations


def test_lockup_maps_to_primary_degraded():
    from backend.core.ouroboros.governance.local_inference_director import LocalLatencyLockup
    from backend.core.ouroboros.governance.candidate_generator import classify_local_failure
    verdict = classify_local_failure(LocalLatencyLockup("timeout"))
    assert verdict.degrade is True
    assert verdict.target_state == "PRIMARY_DEGRADED"
    assert verdict.cascade_upstream is True


def test_normal_exception_does_not_degrade():
    from backend.core.ouroboros.governance.candidate_generator import classify_local_failure
    verdict = classify_local_failure(ValueError("schema"))
    assert verdict.degrade is False
    assert verdict.target_state is None
    assert verdict.cascade_upstream is False

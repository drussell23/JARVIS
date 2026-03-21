"""Tests for ShadowHarness integration in orchestrator and op_context."""
import pytest
from backend.core.ouroboros.governance.shadow_harness import ShadowHarness
from backend.core.ouroboros.governance.op_context import (
    OperationContext, ShadowResult,
)


def _make_ctx():
    return OperationContext.create(
        target_files=("backend/foo.py",),
        description="shadow test",
    )


def test_with_shadow_result_attaches_result():
    """with_shadow_result() must attach a ShadowResult and change context_hash."""
    ctx = _make_ctx()
    original_hash = ctx.context_hash
    sr = ShadowResult(
        confidence=0.9,
        comparison_mode="ast",
        violations=(),
        shadow_duration_s=0.01,
        production_match=True,
        disqualified=False,
    )
    ctx2 = ctx.with_shadow_result(sr)
    assert ctx2.shadow == sr
    assert ctx2.context_hash != original_hash, "hash must change after shadow result"
    assert ctx.shadow is None  # original is immutable


def test_shadow_harness_disqualifies_after_three_low_confidence():
    """ShadowHarness.is_disqualified becomes True after 3 consecutive low-confidence runs."""
    harness = ShadowHarness(confidence_threshold=0.7, disqualify_after=3)
    assert not harness.is_disqualified
    harness.record_run(0.5)
    harness.record_run(0.5)
    assert not harness.is_disqualified
    harness.record_run(0.5)
    assert harness.is_disqualified


def test_orchestrator_config_accepts_shadow_harness():
    """OrchestratorConfig must accept shadow_harness as an optional field."""
    from backend.core.ouroboros.governance.orchestrator import OrchestratorConfig
    import pathlib
    cfg = OrchestratorConfig(project_root=pathlib.Path("."))
    assert cfg.shadow_harness is None  # default is None

    harness = ShadowHarness()
    cfg2 = OrchestratorConfig(project_root=pathlib.Path("."), shadow_harness=harness)
    assert cfg2.shadow_harness is harness

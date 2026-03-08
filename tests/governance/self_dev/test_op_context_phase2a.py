"""Tests for Phase 2A op_context extensions."""
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
    PHASE_TRANSITIONS,
    ValidationResult,
)


def test_validation_result_has_failure_class_field():
    """ValidationResult stores failure_class compactly."""
    vr = ValidationResult(
        passed=False,
        best_candidate=None,
        validation_duration_s=1.0,
        error="tests failed",
        failure_class="test",
        short_summary="1 failed in 0.5s",
        adapter_names_run=("python",),
    )
    assert vr.failure_class == "test"
    assert vr.short_summary == "1 failed in 0.5s"
    assert vr.adapter_names_run == ("python",)


def test_validation_result_defaults_are_lean():
    """ValidationResult new fields default to empty/None — no full output embedded."""
    vr = ValidationResult(
        passed=True,
        best_candidate={"file": "foo.py", "content": "x=1"},
        validation_duration_s=0.5,
        error=None,
    )
    assert vr.failure_class is None
    assert vr.short_summary == ""
    assert vr.adapter_names_run == ()


def test_validate_to_postmortem_is_legal():
    """VALIDATE -> POSTMORTEM is a legal transition (infra failures)."""
    assert OperationPhase.POSTMORTEM in PHASE_TRANSITIONS[OperationPhase.VALIDATE]


def test_validate_retry_to_postmortem_is_legal():
    """VALIDATE_RETRY -> POSTMORTEM is also legal."""
    assert OperationPhase.POSTMORTEM in PHASE_TRANSITIONS[OperationPhase.VALIDATE_RETRY]


def test_operation_context_has_pipeline_deadline_field():
    """OperationContext has pipeline_deadline (Optional[datetime], default None)."""
    ctx = OperationContext.create(
        target_files=("foo.py",),
        description="test",
    )
    assert ctx.pipeline_deadline is None


def test_operation_context_advance_propagates_pipeline_deadline():
    """pipeline_deadline is preserved through advance()."""
    dl = datetime.now(tz=timezone.utc) + timedelta(seconds=300)
    ctx = OperationContext.create(
        target_files=("foo.py",),
        description="test",
        pipeline_deadline=dl,
    )
    ctx2 = ctx.advance(OperationPhase.ROUTE)
    assert ctx2.pipeline_deadline == dl


def test_operation_context_create_with_deadline_is_hashed():
    """Two contexts with different pipeline_deadline have different hashes."""
    dl1 = datetime.now(tz=timezone.utc) + timedelta(seconds=60)
    dl2 = datetime.now(tz=timezone.utc) + timedelta(seconds=120)
    ctx1 = OperationContext.create(target_files=("f.py",), description="x", pipeline_deadline=dl1)
    ctx2 = OperationContext.create(target_files=("f.py",), description="x", pipeline_deadline=dl2)
    assert ctx1.context_hash != ctx2.context_hash

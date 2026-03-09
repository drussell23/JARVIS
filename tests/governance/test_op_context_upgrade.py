"""Tests for OperationContext saga fields (Phase 3)."""
import pytest
from backend.core.ouroboros.governance.op_context import (
    ArchitecturalCycleError,
    OperationContext,
    OperationPhase,
    RepoSagaStatus,
    SagaStepStatus,
)


def test_saga_step_status_values():
    """All required saga step statuses exist."""
    required = {"pending", "applying", "applied", "skipped", "failed",
                "compensating", "compensated", "compensation_failed"}
    assert required == {s.value for s in SagaStepStatus}


def test_repo_saga_status_frozen():
    """RepoSagaStatus is a frozen dataclass."""
    s = RepoSagaStatus(repo="jarvis", status=SagaStepStatus.PENDING)
    with pytest.raises((AttributeError, TypeError)):
        s.repo = "prime"  # type: ignore


def test_repo_saga_status_defaults():
    s = RepoSagaStatus(repo="jarvis", status=SagaStepStatus.PENDING)
    assert s.attempt == 0
    assert s.last_error == ""
    assert s.reason_code == ""
    assert s.compensation_attempted is False


def test_op_context_has_saga_fields():
    """OperationContext.create() includes all new Phase 3 fields."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="test",
        primary_repo="jarvis",
    )
    assert ctx.primary_repo == "jarvis"
    assert ctx.repo_scope == ("jarvis",)
    assert ctx.cross_repo is False
    assert ctx.dependency_edges == ()
    assert ctx.apply_plan == ()
    assert ctx.repo_snapshots == ()
    assert ctx.saga_id == ""
    assert ctx.saga_state == ()
    assert ctx.schema_version == "3.0"


def test_cross_repo_derived_true():
    """cross_repo is True when repo_scope has more than one entry."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="multi",
        repo_scope=("jarvis", "prime"),
        primary_repo="jarvis",
    )
    assert ctx.cross_repo is True


def test_cross_repo_derived_false_single():
    """cross_repo is False for single repo."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="single",
        repo_scope=("jarvis",),
        primary_repo="jarvis",
    )
    assert ctx.cross_repo is False


def test_dag_cycle_raises():
    """Cycle in dependency_edges raises ArchitecturalCycleError at create time."""
    with pytest.raises(ArchitecturalCycleError):
        OperationContext.create(
            target_files=("backend/x.py",),
            description="cyclic",
            repo_scope=("jarvis", "prime"),
            primary_repo="jarvis",
            dependency_edges=(("jarvis", "prime"), ("prime", "jarvis")),
        )


def test_dag_no_cycle_valid():
    """Acyclic dependency_edges is accepted."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="acyclic",
        repo_scope=("jarvis", "prime"),
        primary_repo="jarvis",
        dependency_edges=(("prime", "jarvis"),),
    )
    assert len(ctx.dependency_edges) == 1


def test_schema_version():
    """schema_version is '3.0'."""
    ctx = OperationContext.create(
        target_files=("f.py",), description="d"
    )
    assert ctx.schema_version == "3.0"


def test_advance_preserves_saga_fields():
    """advance() preserves all new fields on phase transitions."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="d",
        primary_repo="prime",
        repo_scope=("jarvis", "prime"),
        saga_id="saga-001",
    )
    ctx2 = ctx.advance(OperationPhase.ROUTE)
    assert ctx2.primary_repo == ctx.primary_repo
    assert ctx2.repo_scope == ctx.repo_scope
    assert ctx2.saga_id == ctx.saga_id
    assert ctx2.cross_repo is True


def test_existing_create_still_works():
    """Existing callers of create() without new kwargs still work."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="legacy call",
    )
    assert ctx.primary_repo == "jarvis"
    assert ctx.schema_version == "3.0"
    assert ctx.cross_repo is False

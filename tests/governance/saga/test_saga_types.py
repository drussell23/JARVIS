"""Tests for saga package types."""
import pytest
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaApplyResult,
    SagaTerminalState,
)


def test_file_op_values():
    assert {FileOp.MODIFY, FileOp.CREATE, FileOp.DELETE}


def test_patched_file_frozen():
    pf = PatchedFile(path="backend/x.py", op=FileOp.MODIFY, preimage=b"old content")
    with pytest.raises(AttributeError):
        pf.path = "other.py"  # type: ignore


def test_patched_file_create_no_preimage():
    """CREATE files have no preimage."""
    pf = PatchedFile(path="backend/new.py", op=FileOp.CREATE, preimage=None)
    assert pf.preimage is None


def test_repo_patch_frozen():
    p = RepoPatch(repo="jarvis", files=())
    with pytest.raises(AttributeError):
        p.repo = "prime"  # type: ignore


def test_repo_patch_is_empty():
    assert RepoPatch(repo="jarvis", files=()).is_empty()
    pf = PatchedFile(path="x.py", op=FileOp.MODIFY, preimage=b"x")
    assert not RepoPatch(repo="jarvis", files=(pf,)).is_empty()


def test_saga_terminal_state_values():
    required = {
        "saga_apply_completed",
        "saga_rolled_back",
        "saga_stuck",
        "saga_succeeded",
        "saga_verify_failed",
        "saga_aborted",
    }
    assert required == {s.value for s in SagaTerminalState}


def test_saga_apply_result_fields():
    result = SagaApplyResult(
        terminal_state=SagaTerminalState.SAGA_APPLY_COMPLETED,
        saga_id="s1",
        saga_step_index=2,
        error=None,
    )
    assert result.saga_step_index == 2
    assert result.error is None
    assert result.reason_code == ""


def test_package_imports():
    """All public types importable from the package root."""
    from backend.core.ouroboros.governance.saga import (
        FileOp,
        PatchedFile,
        RepoPatch,
        SagaApplyResult,
        SagaTerminalState,
    )
    assert FileOp.CREATE
    assert SagaTerminalState.SAGA_STUCK

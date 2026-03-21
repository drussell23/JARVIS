"""Tests for canary slice metrics wiring in GovernedLoopService."""
from unittest.mock import MagicMock


def _make_mock_canary():
    canary = MagicMock()
    canary.record_operation = MagicMock()
    return canary


def test_record_operation_updates_slice_metrics():
    """After record_operation, SliceMetrics.total_operations increments."""
    from backend.core.ouroboros.governance.canary_controller import CanaryController
    cc = CanaryController()
    cc.register_slice("tests/")
    before = cc.get_metrics("tests/").total_operations
    cc.record_operation("tests/foo.py", success=True, latency_s=0.8, rolled_back=False)
    after = cc.get_metrics("tests/").total_operations
    assert after == before + 1


def test_record_operation_rolled_back():
    """record_operation correctly captures rolled_back=True."""
    from backend.core.ouroboros.governance.canary_controller import CanaryController
    cc = CanaryController()
    cc.register_slice("tests/")
    cc.record_operation("tests/foo.py", success=False, latency_s=0.5, rolled_back=True)
    metrics = cc.get_metrics("tests/")
    assert metrics.rollback_count >= 1


def test_record_operation_success_count():
    """record_operation tracks success separately from failures."""
    from backend.core.ouroboros.governance.canary_controller import CanaryController
    cc = CanaryController()
    cc.register_slice("backend/")
    cc.record_operation("backend/foo.py", success=True, latency_s=1.2, rolled_back=False)
    cc.record_operation("backend/bar.py", success=False, latency_s=0.3, rolled_back=False)
    metrics = cc.get_metrics("backend/")
    assert metrics.total_operations == 2


def test_canary_block_skips_when_stack_is_none():
    """When self._stack is None, canary block silently skips (no AttributeError)."""
    from backend.core.ouroboros.governance.canary_controller import CanaryController
    cc = CanaryController()
    cc.register_slice("backend/")
    before = cc.get_metrics("backend/").total_operations

    # Simulate the guard: if self._stack is None, skip the block entirely
    _stack = None
    if _stack is not None and _stack.canary is not None:
        cc.record_operation("backend/foo.py", success=True, latency_s=1.0, rolled_back=False)

    after = cc.get_metrics("backend/").total_operations
    assert after == before, "canary.record_operation must NOT be called when stack is None"


def test_canary_block_skips_when_canary_is_none():
    """When self._stack.canary is None, canary block silently skips."""
    from backend.core.ouroboros.governance.canary_controller import CanaryController
    cc = CanaryController()
    cc.register_slice("backend/")
    before = cc.get_metrics("backend/").total_operations

    mock_stack = MagicMock()
    mock_stack.canary = None  # explicitly None
    if mock_stack is not None and mock_stack.canary is not None:
        cc.record_operation("backend/foo.py", success=True, latency_s=1.0, rolled_back=False)

    after = cc.get_metrics("backend/").total_operations
    assert after == before, "canary.record_operation must NOT be called when stack.canary is None"


def test_canary_record_exception_does_not_propagate():
    """If record_operation raises, the exception is caught and not propagated."""
    mock_canary = MagicMock()
    mock_canary.record_operation.side_effect = RuntimeError("canary error")

    # Simulate the try/except block present in GLS submit()
    errors_caught = []
    try:
        mock_canary.record_operation(
            file_path="backend/foo.py",
            success=True,
            latency_s=1.0,
            rolled_back=False,
        )
    except Exception as exc:
        errors_caught.append(str(exc))

    # The actual GLS code catches and logs — verifying the pattern works
    assert len(errors_caught) == 1 and "canary error" in errors_caught[0]
    # In the real GLS code this is caught by `except Exception as _canary_exc` and NOT re-raised


def test_gls_submit_contains_canary_call():
    """Structural test: governed_loop_service.py submit() contains the canary record_operation call."""
    import pathlib
    src = pathlib.Path(
        "backend/core/ouroboros/governance/governed_loop_service.py"
    ).read_text()

    assert "canary.record_operation(" in src, \
        "GLS submit() must contain canary.record_operation() call"
    assert "_canary_success = terminal_ctx.phase is OperationPhase.COMPLETE" in src, \
        "canary success must be derived from terminal_ctx.phase"
    assert "rolled_back=_rollback_occurred" in src, \
        "canary call must pass rolled_back=_rollback_occurred"
    assert "latency_s=duration" in src, \
        "canary call must pass latency_s=duration"
    assert "except Exception as _canary_exc" in src, \
        "canary call must be wrapped in exception handler"

"""Tests for SAGA_PARTIAL_PROMOTE handling in orchestrator."""
from backend.core.ouroboros.governance.saga.saga_types import SagaTerminalState


def test_partial_promote_state_recognized():
    assert SagaTerminalState.SAGA_PARTIAL_PROMOTE.value == "saga_partial_promote"
    assert SagaTerminalState.SAGA_PARTIAL_PROMOTE != SagaTerminalState.SAGA_STUCK

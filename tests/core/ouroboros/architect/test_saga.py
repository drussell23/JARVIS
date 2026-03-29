"""Tests for SagaRecord and StepState WAL-backed saga schemas."""
from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.architect.saga import (
    SagaPhase,
    SagaRecord,
    StepPhase,
    StepState,
)


# ---------------------------------------------------------------------------
# StepState tests
# ---------------------------------------------------------------------------


class TestStepState:
    def test_step_starts_pending(self):
        step = StepState(step_index=0, phase=StepPhase.PENDING)
        assert step.phase is StepPhase.PENDING
        assert step.envelope_id is None
        assert step.started_at is None
        assert step.completed_at is None
        assert step.error is None

    def test_step_state_transition(self):
        step = StepState(step_index=1, phase=StepPhase.PENDING)
        step.phase = StepPhase.RUNNING
        step.started_at = time.time()
        assert step.phase is StepPhase.RUNNING
        assert step.started_at is not None

    def test_step_state_serialization_roundtrip(self):
        now = time.time()
        step = StepState(
            step_index=3,
            phase=StepPhase.COMPLETE,
            envelope_id="env-abc",
            started_at=now - 10.0,
            completed_at=now,
            error=None,
        )
        d = step.to_dict()
        restored = StepState.from_dict(d)

        assert restored.step_index == step.step_index
        assert restored.phase is StepPhase.COMPLETE
        assert restored.envelope_id == "env-abc"
        assert restored.started_at == pytest.approx(now - 10.0)
        assert restored.completed_at == pytest.approx(now)
        assert restored.error is None

    def test_step_state_with_error_roundtrip(self):
        step = StepState(
            step_index=2,
            phase=StepPhase.FAILED,
            error="timeout exceeded",
        )
        restored = StepState.from_dict(step.to_dict())
        assert restored.phase is StepPhase.FAILED
        assert restored.error == "timeout exceeded"


# ---------------------------------------------------------------------------
# SagaRecord tests
# ---------------------------------------------------------------------------


class TestSagaRecord:
    def _make_saga(self, num_steps: int = 3) -> SagaRecord:
        return SagaRecord.create(
            saga_id="saga-001",
            plan_id="plan-001",
            plan_hash="abc123",
            num_steps=num_steps,
        )

    # --- construction ---

    def test_saga_starts_pending(self):
        saga = self._make_saga(num_steps=3)
        assert saga.phase is SagaPhase.PENDING
        assert len(saga.step_states) == 3
        for idx in range(3):
            assert saga.step_states[idx].phase is StepPhase.PENDING

    def test_saga_metadata_set_correctly(self):
        saga = self._make_saga()
        assert saga.saga_id == "saga-001"
        assert saga.plan_id == "plan-001"
        assert saga.plan_hash == "abc123"
        assert saga.created_at > 0.0
        assert saga.completed_at is None
        assert saga.abort_reason is None

    def test_saga_zero_steps(self):
        saga = SagaRecord.create(
            saga_id="s", plan_id="p", plan_hash="h", num_steps=0
        )
        assert saga.step_states == {}

    # --- all_steps_complete property ---

    def test_all_steps_complete_false_when_pending(self):
        saga = self._make_saga(num_steps=2)
        assert saga.all_steps_complete is False

    def test_all_steps_complete_false_with_mixed(self):
        saga = self._make_saga(num_steps=2)
        saga.step_states[0].phase = StepPhase.COMPLETE
        # step 1 still PENDING
        assert saga.all_steps_complete is False

    def test_saga_all_steps_complete(self):
        saga = self._make_saga(num_steps=2)
        saga.step_states[0].phase = StepPhase.COMPLETE
        saga.step_states[1].phase = StepPhase.COMPLETE
        assert saga.all_steps_complete is True

    def test_saga_not_complete_with_pending(self):
        saga = self._make_saga(num_steps=3)
        saga.step_states[0].phase = StepPhase.COMPLETE
        saga.step_states[1].phase = StepPhase.COMPLETE
        # step 2 still PENDING
        assert saga.all_steps_complete is False

    def test_all_steps_complete_zero_steps(self):
        saga = SagaRecord.create(
            saga_id="s", plan_id="p", plan_hash="h", num_steps=0
        )
        # vacuously true: no steps to block completion
        assert saga.all_steps_complete is True

    # --- has_failed_step property ---

    def test_has_failed_step_false_when_all_pending(self):
        saga = self._make_saga(num_steps=3)
        assert saga.has_failed_step is False

    def test_saga_has_failed_step(self):
        saga = self._make_saga(num_steps=3)
        saga.step_states[1].phase = StepPhase.FAILED
        assert saga.has_failed_step is True

    def test_has_failed_step_false_when_all_complete(self):
        saga = self._make_saga(num_steps=2)
        for s in saga.step_states.values():
            s.phase = StepPhase.COMPLETE
        assert saga.has_failed_step is False

    def test_has_failed_step_blocked_is_not_failed(self):
        saga = self._make_saga(num_steps=2)
        saga.step_states[0].phase = StepPhase.BLOCKED
        assert saga.has_failed_step is False

    # --- serialization ---

    def test_saga_serialization_roundtrip(self):
        saga = self._make_saga(num_steps=3)
        saga.phase = SagaPhase.RUNNING
        saga.step_states[0].phase = StepPhase.COMPLETE
        saga.step_states[0].envelope_id = "env-xyz"
        saga.step_states[0].started_at = saga.created_at + 1.0
        saga.step_states[0].completed_at = saga.created_at + 5.0
        saga.step_states[1].phase = StepPhase.RUNNING
        saga.step_states[1].started_at = saga.created_at + 5.1

        d = saga.to_dict()
        restored = SagaRecord.from_dict(d)

        assert restored.saga_id == saga.saga_id
        assert restored.plan_id == saga.plan_id
        assert restored.plan_hash == saga.plan_hash
        assert restored.phase is SagaPhase.RUNNING
        assert restored.created_at == pytest.approx(saga.created_at)
        assert restored.completed_at is None
        assert restored.abort_reason is None

        assert len(restored.step_states) == 3
        assert restored.step_states[0].phase is StepPhase.COMPLETE
        assert restored.step_states[0].envelope_id == "env-xyz"
        assert restored.step_states[1].phase is StepPhase.RUNNING
        assert restored.step_states[2].phase is StepPhase.PENDING

    def test_saga_serialization_with_abort(self):
        saga = self._make_saga(num_steps=2)
        saga.phase = SagaPhase.ABORTED
        saga.abort_reason = "plan_hash_mismatch"
        saga.completed_at = saga.created_at + 3.0

        restored = SagaRecord.from_dict(saga.to_dict())
        assert restored.phase is SagaPhase.ABORTED
        assert restored.abort_reason == "plan_hash_mismatch"
        assert restored.completed_at == pytest.approx(saga.created_at + 3.0)

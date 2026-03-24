import pytest
from backend.core.runtime_task_orchestrator import (
    ActionOutcome, VerifyTier, StopReason, RuntimeTaskOrchestrator,
)


def test_action_outcome_values():
    assert ActionOutcome.SUCCESS == "success"
    assert ActionOutcome.FAILURE == "failure"
    assert ActionOutcome.UNKNOWN == "unknown"


def test_verify_tier_values():
    assert VerifyTier.EXECUTOR == "executor"
    assert VerifyTier.FRAME_DELTA == "frame_delta"
    assert VerifyTier.MODEL_VERIFY == "model_verify"


def test_stop_reason_values():
    assert StopReason.GOAL_SATISFIED == "goal_satisfied"
    assert StopReason.STAGNATION == "stagnation"
    assert StopReason.MAX_TURNS == "max_turns"
    assert StopReason.MODEL_REFUSAL == "model_refusal"
    assert StopReason.ERROR == "error"


class _FakeAction:
    def __init__(self, action_type, target, text=None):
        self.action_type = action_type
        self.target = target
        self.text = text


def test_stagnation_detects_repeated_successful_action():
    log = [
        {"action_type": "click", "target": "search bar", "text": None, "result": "success", "frame_hash": "a1"},
        {"action_type": "click", "target": "search bar", "text": None, "result": "success", "frame_hash": "a2"},
        {"action_type": "click", "target": "search bar", "text": None, "result": "success", "frame_hash": "a3"},
    ]
    proposed = _FakeAction("click", "search bar")
    assert RuntimeTaskOrchestrator._is_stagnant_static(log, proposed, stagnation_window=3, frame_stagnation=3)


def test_stagnation_ignores_failed_repeats():
    log = [
        {"action_type": "click", "target": "button", "text": None, "result": "failure", "frame_hash": "a1"},
        {"action_type": "click", "target": "button", "text": None, "result": "failure", "frame_hash": "a2"},
        {"action_type": "click", "target": "button", "text": None, "result": "failure", "frame_hash": "a3"},
    ]
    proposed = _FakeAction("click", "button")
    assert not RuntimeTaskOrchestrator._is_stagnant_static(log, proposed, stagnation_window=3, frame_stagnation=3)


def test_stagnation_detects_frozen_frames():
    log = [
        {"action_type": "click", "target": "a", "text": None, "result": "success", "frame_hash": "same"},
        {"action_type": "type", "target": "b", "text": "x", "result": "success", "frame_hash": "same"},
        {"action_type": "scroll", "target": "c", "text": None, "result": "success", "frame_hash": "same"},
    ]
    proposed = _FakeAction("click", "d")
    assert RuntimeTaskOrchestrator._is_stagnant_static(log, proposed, stagnation_window=3, frame_stagnation=3)


def test_stagnation_no_false_positive_on_short_log():
    log = [
        {"action_type": "click", "target": "search bar", "text": None, "result": "success", "frame_hash": "a1"},
    ]
    proposed = _FakeAction("click", "search bar")
    assert not RuntimeTaskOrchestrator._is_stagnant_static(log, proposed, stagnation_window=3, frame_stagnation=3)

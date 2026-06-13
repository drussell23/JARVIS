"""Slice 230 — Iron-Gate rejection feeds model rotation (the missing wire).

ROOT CAUSE (live soak GOAL-001::file-00, layer 6): the sentinel walk stops at
the first TRANSPORT success — and a weak model that instantly emits a no-tool
patch IS a transport success (outcome=ok, 15.7s). The Iron Gate rejects it
LATER (exploration_insufficient), but nothing feeds that rejection back into
dispatch — so GENERATE_RETRY re-walks, picks the same weak model again, fails
identically, and the op dies with the elite agentic pool (V4-Pro/Kimi/GLM,
ranked FIRST by Slice 228/229) sitting unreached behind a model that keeps
"succeeding" at producing garbage.

FIX: reuse the existing Slice-20C rotation mechanism. A new
``DriftType.EXPLORATION_INSUFFICIENT`` is recorded for (op_id, model_id) at the
Iron Gate rejection site, so ``has_drifted`` makes the NEXT walk skip that model
and rotate to the next ranked candidate — the elites. Extending the closed
DriftType enum is the documented "deliberate slice" path; the skip predicate is
type-agnostic so no dispatcher change is needed.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.schema_drift_tracker import (
    DriftType,
    get_default_tracker,
)


def test_drift_type_exploration_insufficient_exists():
    assert DriftType.EXPLORATION_INSUFFICIENT.value == "exploration_insufficient"


def test_gate_drift_recording_rotates_model(monkeypatch):
    """The orchestrator helper records drift → has_drifted skips the model."""
    monkeypatch.setenv("JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED", "1")
    from backend.core.ouroboros.governance.orchestrator import (
        _slice230_record_exploration_drift,
    )
    tracker = get_default_tracker()
    op = "op-s230-test-1"
    assert tracker.has_drifted(op, "weak/Model-A") is False
    _slice230_record_exploration_drift(op, "weak/Model-A")
    assert tracker.has_drifted(op, "weak/Model-A") is True
    # other models for the same op stay eligible (rotation, not abort)
    assert tracker.has_drifted(op, "elite/Model-B") is False


def test_gate_drift_helper_fail_soft(monkeypatch):
    """Empty/garbage inputs never raise (the gate path must not be perturbed)."""
    monkeypatch.setenv("JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED", "1")
    from backend.core.ouroboros.governance.orchestrator import (
        _slice230_record_exploration_drift,
    )
    _slice230_record_exploration_drift("", "")
    _slice230_record_exploration_drift(None, None)  # type: ignore[arg-type]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

"""Slice 1 tests — TrajectoryFrame primitive."""
from __future__ import annotations

import time
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance.trajectory_frame import (
    TRAJECTORY_FRAME_SCHEMA_VERSION,
    Confidence,
    TrajectoryFrame,
    TrajectoryPhase,
    confidence_band,
    idle_frame,
    phase_from_raw,
)


# ===========================================================================
# Schema version
# ===========================================================================


def test_schema_version_stable():
    assert TRAJECTORY_FRAME_SCHEMA_VERSION == "trajectory_frame.v1"


# ===========================================================================
# phase_from_raw mapping
# ===========================================================================


@pytest.mark.parametrize("raw,expected", [
    ("classify", TrajectoryPhase.CLASSIFYING),
    ("CLASSIFY", TrajectoryPhase.CLASSIFYING),
    ("route", TrajectoryPhase.CLASSIFYING),
    ("plan", TrajectoryPhase.PLANNING),
    ("context_expansion", TrajectoryPhase.PLANNING),
    ("generate", TrajectoryPhase.GENERATING),
    ("generate_retry", TrajectoryPhase.GENERATING),
    ("validate", TrajectoryPhase.GENERATING),
    ("gate", TrajectoryPhase.GENERATING),
    ("apply", TrajectoryPhase.APPLYING),
    ("verify", TrajectoryPhase.VERIFYING),
    ("complete", TrajectoryPhase.COMPLETE),
    ("postmortem", TrajectoryPhase.COMPLETE),
    ("idle", TrajectoryPhase.IDLE),
])
def test_phase_mapping(raw: str, expected: TrajectoryPhase):
    assert phase_from_raw(raw) is expected


def test_phase_unknown_falls_through():
    assert phase_from_raw("some-new-phase") is TrajectoryPhase.UNKNOWN


@pytest.mark.parametrize("null_like", ["", None, "   "])
def test_phase_none_empty_returns_unknown(null_like):
    assert phase_from_raw(null_like) is TrajectoryPhase.UNKNOWN


# ===========================================================================
# Confidence bands
# ===========================================================================


@pytest.mark.parametrize("value,band", [
    (0.9, Confidence.HIGH),
    (0.8, Confidence.HIGH),
    (0.65, Confidence.MEDIUM),
    (0.5, Confidence.MEDIUM),
    (0.3, Confidence.LOW),
    (0.2, Confidence.LOW),
    (0.1, Confidence.UNKNOWN),
    (0.0, Confidence.UNKNOWN),
    (None, Confidence.UNKNOWN),
])
def test_confidence_band(value, band):
    assert confidence_band(value) is band


# ===========================================================================
# idle_frame
# ===========================================================================


def test_idle_frame_defaults():
    f = idle_frame(sequence=1)
    assert f.is_idle is True
    assert f.phase is TrajectoryPhase.IDLE
    assert f.has_op is False
    assert f.sequence == 1
    assert f.snapshot_at_iso
    assert f.snapshot_at_ts > 0


def test_idle_frame_one_line_summary():
    f = idle_frame()
    assert f.one_line_summary() == "idle"


def test_idle_frame_narrative():
    f = idle_frame()
    assert f.narrative() == "currently: idle"


# ===========================================================================
# Full frame construction
# ===========================================================================


def _full_frame(**overrides: Any) -> TrajectoryFrame:
    base: Dict[str, Any] = {
        "sequence": 42,
        "snapshot_at_iso": "2026-04-21T10:00:00+00:00",
        "snapshot_at_ts": 1745244000.0,
        "op_id": "op-abc1234567890",
        "phase": TrajectoryPhase.APPLYING,
        "raw_phase": "apply",
        "subject": "fix auth import",
        "target_paths": ("backend/auth.py",),
        "active_tools": ("edit_file",),
        "trigger_source": "test_failure",
        "trigger_reason": "fired for backend/auth.py",
        "started_at_iso": "2026-04-21T09:58:00+00:00",
        "started_at_ts": 1745243880.0,
        "eta_seconds": 45.0,
        "cost_spent_usd": 0.012,
        "cost_budget_usd": 0.50,
        "next_step": "apply candidate to disk",
        "confidence": 0.75,
    }
    base.update(overrides)
    return TrajectoryFrame(**base)


def test_full_frame_has_op_true():
    f = _full_frame()
    assert f.has_op is True
    assert f.is_idle is False


def test_full_frame_confidence_band():
    f = _full_frame(confidence=0.9)
    assert f.confidence_band is Confidence.HIGH
    f2 = _full_frame(confidence=0.25)
    assert f2.confidence_band is Confidence.LOW


def test_cost_remaining_and_used_ratio():
    f = _full_frame(cost_spent_usd=0.10, cost_budget_usd=0.50)
    assert f.cost_remaining_usd == pytest.approx(0.40)
    assert f.cost_used_ratio == pytest.approx(0.20)


def test_cost_over_budget_clamps_remaining_to_zero():
    f = _full_frame(cost_spent_usd=1.0, cost_budget_usd=0.50)
    assert f.cost_remaining_usd == 0.0


def test_cost_without_budget_returns_none():
    f = _full_frame(cost_budget_usd=None)
    assert f.cost_remaining_usd is None
    assert f.cost_used_ratio is None


# ===========================================================================
# one_line_summary + narrative
# ===========================================================================


def test_one_line_summary_has_key_elements():
    line = _full_frame().one_line_summary()
    assert "op-abc1234" in line
    assert "applying" in line
    assert "backend/auth.py" in line
    assert "sensor=test_failure" in line
    assert "ETA 45s" in line
    assert "$0.012" in line


def test_narrative_matches_gap_quote_shape():
    """Gap writeup: `currently: op-X, analyzing path Y because sensor Z
    fired, ETA W seconds, cost $C.`"""
    line = _full_frame().narrative()
    assert line.startswith("currently: op-")
    assert "applying" in line
    assert "backend/auth.py" in line
    assert "because sensor test_failure" in line
    assert "ETA 45s" in line
    assert "cost" in line.lower()
    assert "$0.012" in line


def test_narrative_multi_path():
    f = _full_frame(
        target_paths=("a.py", "b.py", "c.py"),
    )
    line = f.narrative()
    assert "3 paths" in line
    assert "(a.py" in line


def test_narrative_without_trigger():
    f = _full_frame(trigger_source="", trigger_reason="")
    line = f.narrative()
    assert "because" not in line


def test_narrative_without_eta():
    f = _full_frame(eta_seconds=None)
    line = f.narrative()
    assert "ETA" not in line


def test_narrative_without_cost():
    f = _full_frame(cost_spent_usd=0.0, cost_budget_usd=None)
    line = f.narrative()
    assert "cost" not in line.lower()


def test_narrative_blocked_marker():
    f = _full_frame(is_blocked=True, blocked_reason="awaiting approval")
    line = f.narrative()
    assert "[BLOCKED" in line
    assert "awaiting approval" in line


def test_narrative_includes_next_step():
    line = _full_frame().narrative()
    assert "→" in line
    assert "apply candidate to disk" in line


# ===========================================================================
# Duration + cost formatting
# ===========================================================================


@pytest.mark.parametrize("seconds,expected", [
    (30.0, "30s"),
    (59.9, "59s"),
    (60.0, "1m"),
    (120.0, "2m"),
    (3599.0, "59m"),
    (3600.0, "1.0h"),
    (7200.0, "2.0h"),
])
def test_fmt_duration(seconds: float, expected: str):
    from backend.core.ouroboros.governance.trajectory_frame import _fmt_duration
    assert _fmt_duration(seconds) == expected


def test_fmt_cost_no_budget():
    from backend.core.ouroboros.governance.trajectory_frame import _fmt_cost
    assert _fmt_cost(0.012, None) == "$0.012"
    assert _fmt_cost(0.0, None) == ""


def test_fmt_cost_with_budget():
    from backend.core.ouroboros.governance.trajectory_frame import _fmt_cost
    assert _fmt_cost(0.012, 0.50) == "$0.012 / $0.50"


# ===========================================================================
# Projection shape
# ===========================================================================


def test_project_shape_idle():
    f = idle_frame()
    p = f.project()
    assert p["schema_version"] == TRAJECTORY_FRAME_SCHEMA_VERSION
    assert p["is_idle"] is True
    assert p["has_op"] is False
    assert p["phase"] == "idle"
    assert p["one_line_summary"] == "idle"


def test_project_shape_full():
    f = _full_frame()
    p = f.project()
    assert p["op_id"] == "op-abc1234567890"
    assert p["phase"] == "applying"
    assert p["trigger_source"] == "test_failure"
    assert p["eta_seconds"] == 45.0
    assert p["cost_used_ratio"] == pytest.approx(0.024)
    assert "one_line_summary" in p
    assert "narrative" in p


def test_project_bounds_subject_length():
    f = _full_frame(subject="x" * 500)
    p = f.project()
    assert len(p["subject"]) <= 200


def test_project_caps_paths_list_size():
    paths = tuple(f"p{i}.py" for i in range(50))
    f = _full_frame(target_paths=paths)
    p = f.project()
    assert len(p["target_paths"]) <= 20


def test_project_caps_tools_list_size():
    tools = tuple(f"tool_{i}" for i in range(50))
    f = _full_frame(active_tools=tools)
    p = f.project()
    assert len(p["active_tools"]) <= 20


def test_project_bounds_blocked_reason():
    f = _full_frame(is_blocked=True, blocked_reason="x" * 500)
    p = f.project()
    assert len(p["blocked_reason"]) <= 200


def test_project_bounds_next_step():
    f = _full_frame(next_step="x" * 2000)
    p = f.project()
    assert len(p["next_step"]) <= 500


# ===========================================================================
# Immutability + equality
# ===========================================================================


def test_frame_is_frozen():
    f = idle_frame()
    with pytest.raises(Exception):
        f.op_id = "changed"  # type: ignore[misc]


def test_frames_equal_by_value():
    a = _full_frame()
    b = _full_frame()
    assert a == b


def test_frames_hashable():
    f = idle_frame()
    # Hashable frozen dataclass
    assert hash(f) == hash(f)


# ===========================================================================
# Edge cases
# ===========================================================================


def test_empty_op_id_treated_as_no_op():
    f = TrajectoryFrame(op_id="")
    assert f.has_op is False


def test_idle_flag_overrides_op_id_for_has_op():
    f = TrajectoryFrame(op_id="op-x", is_idle=True)
    assert f.has_op is False


def test_short_op_id_not_truncated_aggressively():
    from backend.core.ouroboros.governance.trajectory_frame import _short_op_id
    assert _short_op_id("op-x") == "op-x"


def test_empty_op_id_fallback_string():
    from backend.core.ouroboros.governance.trajectory_frame import _short_op_id
    assert _short_op_id("") == "op-?"

"""Tests for session_diff.py (extension Slice 1)."""
from __future__ import annotations

import json
from pathlib import Path

from backend.core.ouroboros.governance.session_diff import (
    SESSION_DIFF_SCHEMA_VERSION,
    FieldDelta,
    SessionDiff,
    diff_records,
    render_session_diff,
)
from backend.core.ouroboros.governance.session_record import (
    SessionRecord,
    parse_session_dir,
)


# ===========================================================================
# schema version
# ===========================================================================


def test_schema_version_pinned():
    assert SESSION_DIFF_SCHEMA_VERSION == "session_diff.v1"


# ===========================================================================
# diff_records — numeric deltas
# ===========================================================================


def test_diff_all_numeric_fields_present():
    """Every _NUMERIC_FIELDS entry shows up as a FieldDelta."""
    left = SessionRecord(session_id="a", ops_total=5, ops_applied=3)
    right = SessionRecord(session_id="b", ops_total=10, ops_applied=8)
    d = diff_records(left, right)
    names = [f.name for f in d.numeric_deltas]
    # Canonical ordered set.
    assert names == [
        "ops_total", "ops_applied", "ops_verified_pass",
        "ops_verified_total", "duration_s", "cost_spent_usd",
        "on_disk_bytes",
    ]


def test_diff_ops_applied_higher_is_improvement():
    left = SessionRecord(
        session_id="a", ops_total=3, ops_applied=1,
        summary_found=True, stop_reason="complete",
    )
    right = SessionRecord(
        session_id="b", ops_total=5, ops_applied=5,
        summary_found=True, stop_reason="complete",
    )
    d = diff_records(left, right)
    # ops_applied improved
    d_map = {f.name: f for f in d.numeric_deltas}
    assert d_map["ops_applied"].improved is True
    assert d_map["ops_applied"].regressed is False
    assert "ops_applied" in d.improved_fields


def test_diff_cost_higher_is_regression():
    left = SessionRecord(
        session_id="a", ops_total=1, cost_spent_usd=0.10,
        summary_found=True, stop_reason="complete",
    )
    right = SessionRecord(
        session_id="b", ops_total=1, cost_spent_usd=0.50,
        summary_found=True, stop_reason="complete",
    )
    d = diff_records(left, right)
    d_map = {f.name: f for f in d.numeric_deltas}
    assert d_map["cost_spent_usd"].regressed is True
    assert "cost_spent_usd" in d.regressed_fields


def test_diff_zero_delta_is_neither_regressed_nor_improved():
    left = SessionRecord(session_id="a", ops_total=5)
    right = SessionRecord(session_id="b", ops_total=5)
    d = diff_records(left, right)
    d_map = {f.name: f for f in d.numeric_deltas}
    assert d_map["ops_total"].delta == 0.0
    assert d_map["ops_total"].regressed is False
    assert d_map["ops_total"].improved is False


def test_diff_parse_error_suppresses_classification():
    """If either side failed to parse, we don't classify regressions."""
    left = SessionRecord(session_id="a", parse_error=True, ops_total=10)
    right = SessionRecord(
        session_id="b", summary_found=True, stop_reason="complete",
        ops_total=20, ops_applied=20,
    )
    d = diff_records(left, right)
    assert d.improved_fields == ()
    assert d.regressed_fields == ()
    # Numeric deltas still computed
    d_map = {f.name: f for f in d.numeric_deltas}
    assert d_map["ops_total"].delta == 10.0


def test_diff_ok_outcome_flip_classified():
    """When one side is ok and the other is not, ok_outcome appears
    in the classification tuple."""
    left = SessionRecord(
        session_id="a", summary_found=True, ops_total=1,
        stop_reason="complete",
    )
    right = SessionRecord(
        session_id="b", summary_found=True, ops_total=1,
        stop_reason="",  # different non-ok stop_reason
    )
    # Ensure one side's ok_outcome differs.
    # Left ok_outcome = True (summary_found + ops>0 + stop in allowed set)
    # Right ok_outcome = True too — same heuristic passes.
    # Force divergence:
    right = SessionRecord(
        session_id="b", summary_found=True, ops_total=0,
        stop_reason="complete",
    )
    assert left.ok_outcome != right.ok_outcome
    d = diff_records(left, right)
    assert (
        "ok_outcome" in d.regressed_fields
        or "ok_outcome" in d.improved_fields
    )


# ===========================================================================
# diff_records — pair snapshots
# ===========================================================================


def test_diff_pairs_echo_raw_values():
    left = SessionRecord(
        session_id="a", stop_reason="complete", commit_hash="abc123",
        has_replay_html=True,
    )
    right = SessionRecord(
        session_id="b", stop_reason="cost_cap", commit_hash="def456",
        has_replay_html=False,
    )
    d = diff_records(left, right)
    assert d.stop_reason_pair == ("complete", "cost_cap")
    assert d.commit_hash_pair == ("abc123", "def456")
    assert d.has_replay_pair == (True, False)
    assert d.left_session_id == "a"
    assert d.right_session_id == "b"


def test_diff_is_frozen():
    """SessionDiff is immutable."""
    left = SessionRecord(session_id="a")
    right = SessionRecord(session_id="b")
    d = diff_records(left, right)
    try:
        d.left_session_id = "mutated"  # type: ignore[misc]
    except (AttributeError, TypeError):
        pass
    else:
        raise AssertionError("SessionDiff should be frozen")


def test_diff_round_trip_through_project_is_json_safe():
    left = SessionRecord(session_id="a", ops_total=5)
    right = SessionRecord(session_id="b", ops_total=6)
    d = diff_records(left, right)
    p = d.project()
    blob = json.dumps(p)
    assert "session_diff.v1" in blob
    assert "numeric_deltas" in p


# ===========================================================================
# render_session_diff — REPL shape
# ===========================================================================


def test_render_contains_both_session_ids():
    left = SessionRecord(session_id="bt-left-1", ops_total=5)
    right = SessionRecord(session_id="bt-right-2", ops_total=10)
    out = render_session_diff(diff_records(left, right))
    assert "bt-left-1" in out
    assert "bt-right-2" in out
    assert "Session diff" in out


def test_render_marks_regressed_and_improved():
    left = SessionRecord(
        session_id="a", summary_found=True, ops_total=2,
        ops_applied=2, cost_spent_usd=0.10, stop_reason="complete",
    )
    right = SessionRecord(
        session_id="b", summary_found=True, ops_total=2,
        ops_applied=5, cost_spent_usd=0.25, stop_reason="complete",
    )
    out = render_session_diff(diff_records(left, right))
    # both markers present in some form
    assert "^" in out  # improved
    assert "v" in out  # regressed
    assert "regressed" in out
    assert "improved" in out


def test_render_shows_parse_error_when_either_side_is_errored():
    left = SessionRecord(session_id="a", parse_error=True)
    right = SessionRecord(session_id="b")
    out = render_session_diff(diff_records(left, right))
    assert "parse_error" in out


def test_render_never_raises_on_empty_records():
    left = SessionRecord()
    right = SessionRecord()
    out = render_session_diff(diff_records(left, right))
    assert out  # non-empty


# ===========================================================================
# Integration with parse_session_dir
# ===========================================================================


def test_diff_works_with_real_parsed_records(tmp_path: Path):
    """End-to-end: parse two session dirs, diff them."""
    left_dir = tmp_path / "bt-left"
    right_dir = tmp_path / "bt-right"
    left_dir.mkdir()
    right_dir.mkdir()
    (left_dir / "summary.json").write_text(json.dumps({
        "stop_reason": "complete",
        "stats": {"ops_total": 3, "ops_applied": 2, "cost": {"spent_usd": 0.10}},
    }))
    (right_dir / "summary.json").write_text(json.dumps({
        "stop_reason": "complete",
        "stats": {"ops_total": 5, "ops_applied": 5, "cost": {"spent_usd": 0.25}},
    }))
    left = parse_session_dir(left_dir)
    right = parse_session_dir(right_dir)
    d = diff_records(left, right)
    d_map = {f.name: f for f in d.numeric_deltas}
    assert d_map["ops_total"].delta == 2.0
    assert d_map["ops_applied"].delta == 3.0
    assert d_map["ops_applied"].improved
    assert d_map["cost_spent_usd"].regressed


# ===========================================================================
# Determinism
# ===========================================================================


def test_diff_is_deterministic():
    left = SessionRecord(session_id="a", ops_total=7)
    right = SessionRecord(session_id="b", ops_total=12)
    d1 = diff_records(left, right)
    d2 = diff_records(left, right)
    assert d1 == d2


# ===========================================================================
# FieldDelta frozen
# ===========================================================================


def test_field_delta_frozen():
    f = FieldDelta(
        name="ops_total", left=1, right=2, delta=1.0,
        regressed=False, improved=True,
    )
    try:
        f.name = "other"  # type: ignore[misc]
    except (AttributeError, TypeError):
        pass
    else:
        raise AssertionError("FieldDelta should be frozen")

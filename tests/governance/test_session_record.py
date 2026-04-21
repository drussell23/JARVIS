"""Slice 1 tests — SessionRecord + parser."""
from __future__ import annotations

import json
import os
import textwrap
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance.session_record import (
    SESSION_RECORD_SCHEMA_VERSION,
    SessionRecord,
    default_sessions_root,
    parse_session_dir,
)


# ===========================================================================
# Schema version
# ===========================================================================


def test_schema_version_stable():
    assert SESSION_RECORD_SCHEMA_VERSION == "session_record.v1"


# ===========================================================================
# default_sessions_root env knob
# ===========================================================================


def test_default_sessions_root_from_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JARVIS_OUROBOROS_SESSIONS_ROOT", str(tmp_path))
    root = default_sessions_root()
    assert root == tmp_path.resolve()


def test_default_sessions_root_without_env(monkeypatch):
    monkeypatch.delenv("JARVIS_OUROBOROS_SESSIONS_ROOT", raising=False)
    root = default_sessions_root()
    assert root.name == "sessions"


# ===========================================================================
# Fixtures
# ===========================================================================


def _write_summary(
    session_dir: Path,
    data: Dict[str, Any],
) -> Path:
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "summary.json"
    path.write_text(json.dumps(data))
    return path


def _full_summary() -> Dict[str, Any]:
    return {
        "schema_version": "summary.v2",
        "session_id": "bt-2026-04-21-120000",
        "stop_reason": "idle_timeout",
        "started_at": "2026-04-21T12:00:00+00:00",
        "ended_at": "2026-04-21T12:05:00+00:00",
        "duration_s": 300.0,
        "commit_hash": "abc1234567",
        "stats": {
            "ops_total": 12,
            "ops_applied": 5,
            "verify": {"pass": 5, "total": 5},
            "cost": {
                "spent_usd": 0.243,
                "budget_usd": 0.50,
            },
        },
    }


# ===========================================================================
# Happy-path parsing
# ===========================================================================


def test_parse_full_summary(tmp_path: Path):
    session_dir = tmp_path / "bt-2026-04-21-120000"
    _write_summary(session_dir, _full_summary())
    rec = parse_session_dir(session_dir)
    assert rec.session_id == "bt-2026-04-21-120000"
    assert rec.summary_found is True
    assert rec.parse_error is False
    assert rec.stop_reason == "idle_timeout"
    assert rec.ops_total == 12
    assert rec.ops_applied == 5
    assert rec.ops_verified_pass == 5
    assert rec.ops_verified_total == 5
    assert rec.cost_spent_usd == pytest.approx(0.243)
    assert rec.cost_budget_usd == pytest.approx(0.50)
    assert rec.commit_hash == "abc1234567"
    assert rec.duration_s == 300.0
    assert rec.has_debug_log is False
    assert rec.has_replay_html is False


def test_parse_minimal_summary(tmp_path: Path):
    session_dir = tmp_path / "bt-minimal"
    _write_summary(session_dir, {"stop_reason": "cost_cap"})
    rec = parse_session_dir(session_dir)
    assert rec.summary_found is True
    assert rec.stop_reason == "cost_cap"
    assert rec.ops_total == 0
    assert rec.cost_budget_usd is None


def test_parse_toplevel_fallbacks(tmp_path: Path):
    """Some summaries put counters at top-level instead of under stats."""
    session_dir = tmp_path / "bt-toplevel"
    _write_summary(session_dir, {
        "stop_reason": "complete",
        "ops_total": 3,
        "ops_applied": 2,
        "cost_spent_usd": 0.012,
    })
    rec = parse_session_dir(session_dir)
    assert rec.ops_total == 3
    assert rec.ops_applied == 2
    assert rec.cost_spent_usd == pytest.approx(0.012)


def test_parse_schema_version_captured(tmp_path: Path):
    session_dir = tmp_path / "bt-v2"
    _write_summary(session_dir, {"schema_version": "summary.v2"})
    rec = parse_session_dir(session_dir)
    assert rec.schema_version_summary == "summary.v2"


# ===========================================================================
# Debug log head + replay HTML
# ===========================================================================


def test_debug_log_head_captured(tmp_path: Path):
    session_dir = tmp_path / "bt-logs"
    _write_summary(session_dir, _full_summary())
    log_path = session_dir / "debug.log"
    log_path.write_text(
        "line 1\nline 2\nline 3\nline 4\nline 5\n",
    )
    rec = parse_session_dir(session_dir, debug_log_head_lines=3)
    assert rec.has_debug_log is True
    assert len(rec.debug_log_head_lines) == 3
    assert rec.debug_log_head_lines[0] == "line 1"


def test_debug_log_head_lines_capped(tmp_path: Path):
    session_dir = tmp_path / "bt-cap"
    _write_summary(session_dir, _full_summary())
    (session_dir / "debug.log").write_text("x" * 2000 + "\n")
    rec = parse_session_dir(session_dir)
    # Each line capped at 500 chars
    assert all(len(l) <= 500 for l in rec.debug_log_head_lines)


def test_no_debug_log_means_empty_head(tmp_path: Path):
    session_dir = tmp_path / "bt-no-log"
    _write_summary(session_dir, _full_summary())
    rec = parse_session_dir(session_dir)
    assert rec.has_debug_log is False
    assert rec.debug_log_head_lines == ()


def test_replay_html_detected(tmp_path: Path):
    session_dir = tmp_path / "bt-replay"
    _write_summary(session_dir, _full_summary())
    (session_dir / "replay.html").write_text("<html></html>")
    rec = parse_session_dir(session_dir)
    assert rec.has_replay_html is True
    assert rec.replay_html_path.endswith("replay.html")


def test_replay_html_alternate_names(tmp_path: Path):
    session_dir = tmp_path / "bt-altnames"
    _write_summary(session_dir, _full_summary())
    (session_dir / "session_replay.html").write_text("<html></html>")
    rec = parse_session_dir(session_dir)
    assert rec.has_replay_html is True


# ===========================================================================
# Fail-closed behaviour
# ===========================================================================


def test_missing_directory_returns_parse_error(tmp_path: Path):
    rec = parse_session_dir(tmp_path / "does-not-exist")
    assert rec.parse_error is True
    assert rec.parse_error_reason == "not_a_directory"


def test_non_directory_input_returns_parse_error(tmp_path: Path):
    file_path = tmp_path / "not_a_dir"
    file_path.write_text("x")
    rec = parse_session_dir(file_path)
    assert rec.parse_error is True
    assert rec.parse_error_reason == "not_a_directory"


def test_bad_session_id_regex_rejected(tmp_path: Path):
    session_dir = tmp_path / "has spaces!"
    session_dir.mkdir()
    rec = parse_session_dir(session_dir)
    assert rec.parse_error is True
    assert "bad_session_id_format" in rec.parse_error_reason


def test_corrupt_summary_json_captured(tmp_path: Path):
    session_dir = tmp_path / "bt-corrupt"
    session_dir.mkdir()
    (session_dir / "summary.json").write_text("not valid json {{{{")
    rec = parse_session_dir(session_dir)
    assert rec.summary_found is True
    assert rec.parse_error is True
    assert "json_decode_error" in rec.parse_error_reason


def test_missing_summary_produces_unblessed_record(tmp_path: Path):
    session_dir = tmp_path / "bt-no-summary"
    session_dir.mkdir()
    rec = parse_session_dir(session_dir)
    assert rec.summary_found is False
    assert rec.parse_error is False
    assert rec.session_id == "bt-no-summary"


def test_summary_not_mapping_rejected(tmp_path: Path):
    session_dir = tmp_path / "bt-list"
    session_dir.mkdir()
    (session_dir / "summary.json").write_text("[1,2,3]")
    rec = parse_session_dir(session_dir)
    assert rec.parse_error is True
    assert "summary_not_a_mapping" in rec.parse_error_reason


def test_wrong_type_fields_coerce_to_sentinels(tmp_path: Path):
    session_dir = tmp_path / "bt-wrongtype"
    _write_summary(session_dir, {
        "stats": {
            "ops_total": "not-a-number",
            "ops_applied": True,  # bool sentinel
            "cost": {"spent_usd": "nope"},
        },
    })
    rec = parse_session_dir(session_dir)
    # All type coercions fall back to defaults without raising
    assert rec.ops_total == 0
    assert rec.ops_applied == 0
    assert rec.cost_spent_usd == 0.0


# ===========================================================================
# Filesystem metadata
# ===========================================================================


def test_mtime_captured(tmp_path: Path):
    session_dir = tmp_path / "bt-mtime"
    _write_summary(session_dir, _full_summary())
    rec = parse_session_dir(session_dir)
    assert rec.mtime_ts > 0
    assert rec.mtime_iso  # non-empty ISO string


def test_on_disk_bytes_computed(tmp_path: Path):
    session_dir = tmp_path / "bt-bytes"
    _write_summary(session_dir, _full_summary())
    (session_dir / "debug.log").write_text("x" * 1000)
    rec = parse_session_dir(session_dir)
    assert rec.on_disk_bytes >= 1000


# ===========================================================================
# short_session_id + ok_outcome + one_line_summary
# ===========================================================================


def test_short_session_id_truncates_long():
    rec = SessionRecord(session_id="bt-2026-04-21-aaaa-bbbb")
    short = rec.short_session_id
    assert len(short) <= 17  # 16 + the horizontal-ellipsis
    assert "…" in short


def test_short_session_id_preserves_short():
    rec = SessionRecord(session_id="bt-short")
    assert rec.short_session_id == "bt-short"


def test_ok_outcome_true_for_healthy_run(tmp_path: Path):
    session_dir = tmp_path / "bt-ok"
    _write_summary(session_dir, _full_summary())
    rec = parse_session_dir(session_dir)
    assert rec.ok_outcome is True


def test_ok_outcome_false_on_parse_error():
    rec = SessionRecord(
        session_id="bt-bad", parse_error=True,
        ops_total=5, summary_found=True,
    )
    assert rec.ok_outcome is False


def test_ok_outcome_false_on_no_summary():
    rec = SessionRecord(session_id="bt-empty", summary_found=False)
    assert rec.ok_outcome is False


def test_ok_outcome_false_on_zero_ops():
    rec = SessionRecord(
        session_id="bt-empty", summary_found=True,
        ops_total=0, stop_reason="complete",
    )
    assert rec.ok_outcome is False


def test_one_line_summary_ok(tmp_path: Path):
    session_dir = tmp_path / "bt-2026-04-21-001122"
    _write_summary(session_dir, _full_summary())
    rec = parse_session_dir(session_dir)
    line = rec.one_line_summary()
    assert "ok" in line
    assert "ops=12" in line
    assert "applied=5" in line
    assert "verify=5/5" in line
    assert "$0.243" in line


def test_one_line_summary_parse_error():
    rec = SessionRecord(
        session_id="bt-bad", parse_error=True,
        parse_error_reason="json_decode_error",
    )
    line = rec.one_line_summary()
    assert "PARSE-ERROR" in line


def test_one_line_summary_no_summary():
    rec = SessionRecord(session_id="bt-empty", summary_found=False)
    line = rec.one_line_summary()
    assert "no-summary" in line


def test_one_line_summary_replay_marker(tmp_path: Path):
    session_dir = tmp_path / "bt-with-replay"
    _write_summary(session_dir, _full_summary())
    (session_dir / "replay.html").write_text("<html/>")
    rec = parse_session_dir(session_dir)
    line = rec.one_line_summary()
    assert "replay" in line


def test_one_line_summary_empty_record():
    rec = SessionRecord()
    assert rec.one_line_summary() == "<unnamed session>"


# ===========================================================================
# Projection bounds
# ===========================================================================


def test_project_shape_full(tmp_path: Path):
    session_dir = tmp_path / "bt-proj"
    _write_summary(session_dir, _full_summary())
    rec = parse_session_dir(session_dir)
    p = rec.project()
    assert p["schema_version"] == SESSION_RECORD_SCHEMA_VERSION
    assert p["session_id"] == "bt-proj"
    assert p["ops_total"] == 12
    assert "ok_outcome" in p
    assert len(p["commit_hash"]) <= 10


def test_project_empty_record_is_safe():
    p = SessionRecord().project()
    assert p["summary_found"] is False
    assert p["parse_error"] is False
    assert p["ops_total"] == 0


# ===========================================================================
# Immutability
# ===========================================================================


def test_record_is_frozen():
    rec = SessionRecord(session_id="bt-x")
    with pytest.raises(Exception):
        rec.session_id = "bt-y"  # type: ignore[misc]


# ===========================================================================
# Stable sorting
# ===========================================================================


def test_records_equal_by_value(tmp_path: Path):
    session_dir = tmp_path / "bt-equal"
    _write_summary(session_dir, _full_summary())
    r1 = parse_session_dir(session_dir)
    r2 = parse_session_dir(session_dir)
    # mtime may tick but most fields should match
    assert r1.session_id == r2.session_id
    assert r1.ops_total == r2.ops_total
    assert r1.cost_spent_usd == r2.cost_spent_usd

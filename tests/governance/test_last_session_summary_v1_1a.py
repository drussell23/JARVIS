"""LastSessionSummary v1.1a tests — schema_version=2 + ops_digest enrichment.

Covers the v1.1a DoD: parse/render of the new ``ops_digest`` sub-dict
with APPLY / VERIFY / commit typed fields, backward compatibility with
v1 files, graceful degradation on malformed input, and round-trip
through ``SessionRecorder`` + ``OpsDigestObserver``.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance import (
    last_session_summary as lss,
    ops_digest_observer as odo,
)
from backend.core.ouroboros.battle_test.session_recorder import (
    SCHEMA_VERSION,
    SessionRecorder,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_LAST_SESSION_SUMMARY_"):
            monkeypatch.delenv(key, raising=False)
    lss.reset_default_summary()
    lss.set_active_session_id(None)
    odo.reset_ops_digest_observer()
    yield
    lss.reset_default_summary()
    lss.set_active_session_id(None)
    odo.reset_ops_digest_observer()


def _enable(monkeypatch, **overrides):
    monkeypatch.setenv("JARVIS_LAST_SESSION_SUMMARY_ENABLED", "true")
    for k, v in overrides.items():
        monkeypatch.setenv(f"JARVIS_LAST_SESSION_SUMMARY_{k}", str(v))


def _write(
    root: Path,
    session_id: str,
    *,
    schema_version: int = 2,
    ops_digest: Any = None,  # None, {}, or dict
    **overrides: Any,
) -> Path:
    """Write a v1 or v2 summary.json with optional ops_digest."""
    payload: Dict[str, Any] = {
        "session_id": session_id,
        "stop_reason": "idle_timeout",
        "duration_s": 300.0,
        "stats": {
            "attempted": 2, "completed": 1, "failed": 0,
            "cancelled": 0, "queued": 0,
        },
        "cost_total": 0.1,
        "cost_breakdown": {"claude": 0.1},
        "branch_stats": {
            "commits": 1, "files_changed": 4,
            "insertions": 200, "deletions": 50,
        },
        "strategic_drift": {"ratio": 0.0, "status": "ok"},
        "convergence_state": "IMPROVING",
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version
    if ops_digest is not None:
        payload["ops_digest"] = ops_digest
    payload.update(overrides)

    session_dir = root / ".ouroboros" / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "summary.json"
    path.write_text(json.dumps(payload))
    return path


# ---------------------------------------------------------------------------
# (1) v1 summary (no schema_version) parses, v1.1a fields all None
# ---------------------------------------------------------------------------


def test_v1_summary_parses_with_v1_1a_fields_none(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write(
        tmp_path, "bt-2026-04-15-090000",
        schema_version=None,   # v1 file — no schema_version, no ops_digest
    )
    summary = lss.LastSessionSummary(tmp_path)
    records = summary.load()
    assert len(records) == 1
    r = records[0]
    assert r.last_apply_mode is None
    assert r.last_apply_files is None
    assert r.last_apply_op_id == ""
    assert r.last_verify_tests_passed is None
    assert r.last_verify_tests_total is None
    assert r.last_commit_hash == ""

    # Render has no apply/verify/commit tokens — pure v1 line shape.
    prompt = summary.format_for_prompt() or ""
    assert "apply=" not in prompt
    assert "verify=" not in prompt
    assert "commit=" not in prompt


# ---------------------------------------------------------------------------
# (2) v2 full ops_digest — apply + verify + commit all render
# ---------------------------------------------------------------------------


def test_v2_full_ops_digest_renders_all_tokens(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write(
        tmp_path, "bt-2026-04-15-100000",
        ops_digest={
            "last_apply_mode": "multi",
            "last_apply_files": 4,
            "last_apply_op_id": "op-019d94a8-abc",
            "last_verify_tests_passed": 20,
            "last_verify_tests_total": 20,
            "last_commit_hash": "0890a7b6f09123456789abcdef",
        },
    )
    summary = lss.LastSessionSummary(tmp_path)
    prompt = summary.format_for_prompt() or ""
    assert "apply=multi/4" in prompt
    assert "verify=20/20" in prompt
    # Hash truncated to 10 chars.
    assert "commit=0890a7b6f0" in prompt
    assert "0890a7b6f09123" not in prompt  # 14-char hash substring not rendered


# ---------------------------------------------------------------------------
# (3) v2 empty ops_digest renders v1 line (no enrichment)
# ---------------------------------------------------------------------------


def test_v2_empty_ops_digest_renders_v1_line(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write(tmp_path, "bt-2026-04-15-110000", ops_digest={})
    summary = lss.LastSessionSummary(tmp_path)
    prompt = summary.format_for_prompt() or ""
    assert "apply=" not in prompt
    assert "verify=" not in prompt
    assert "commit=" not in prompt
    # Session id still renders — we're in the v1 line shape.
    assert "bt-2026-04-15-110000" in prompt


# ---------------------------------------------------------------------------
# (4) last_apply_mode="none" omits apply= from render
# ---------------------------------------------------------------------------


def test_apply_mode_none_omits_from_render(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write(
        tmp_path, "bt-2026-04-15-120000",
        ops_digest={
            "last_apply_mode": "none",
            "last_apply_files": 0,
        },
    )
    summary = lss.LastSessionSummary(tmp_path)
    prompt = summary.format_for_prompt() or ""
    assert "apply=" not in prompt


# ---------------------------------------------------------------------------
# (5) Partial ops_digest — apply only, no verify/commit
# ---------------------------------------------------------------------------


def test_partial_ops_digest_renders_only_present_tokens(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write(
        tmp_path, "bt-2026-04-15-130000",
        ops_digest={
            "last_apply_mode": "single",
            "last_apply_files": 1,
            # Intentionally no verify, no commit
        },
    )
    summary = lss.LastSessionSummary(tmp_path)
    prompt = summary.format_for_prompt() or ""
    assert "apply=single/1" in prompt
    assert "verify=" not in prompt
    assert "commit=" not in prompt


# ---------------------------------------------------------------------------
# (6) Commit hash truncation to 10 chars
# ---------------------------------------------------------------------------


def test_commit_hash_truncated_to_ten_chars(monkeypatch, tmp_path):
    _enable(monkeypatch)
    full_hash = "abcdef0123456789abcdef0123456789abcdef01"  # 40-char SHA-1
    _write(
        tmp_path, "bt-2026-04-15-140000",
        ops_digest={"last_commit_hash": full_hash},
    )
    summary = lss.LastSessionSummary(tmp_path)
    records = summary.load()
    # Full hash retained in SessionRecord.
    assert records[0].last_commit_hash == full_hash
    # Only 10 chars in prompt render.
    prompt = summary.format_for_prompt() or ""
    assert "commit=abcdef0123 " in prompt or "commit=abcdef0123\n" in prompt or prompt.endswith("commit=abcdef0123")
    assert full_hash not in prompt


# ---------------------------------------------------------------------------
# (7) Malformed ops_digest (not a dict) → graceful, no crash
# ---------------------------------------------------------------------------


def test_malformed_ops_digest_not_a_dict(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write(
        tmp_path, "bt-2026-04-15-150000",
        ops_digest="this should be a dict, not a string",  # type: ignore[arg-type]
    )
    summary = lss.LastSessionSummary(tmp_path)
    records = summary.load()
    assert len(records) == 1
    r = records[0]
    # All v1.1a fields fall back to absent/None.
    assert r.last_apply_mode is None
    assert r.last_commit_hash == ""


# ---------------------------------------------------------------------------
# (8) Per-field type-cast failures → graceful per-field degradation
# ---------------------------------------------------------------------------


def test_bad_field_types_degrade_gracefully(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write(
        tmp_path, "bt-2026-04-15-160000",
        ops_digest={
            "last_apply_mode": "multi",
            "last_apply_files": "four",           # should be int — degrades to None
            "last_verify_tests_passed": -5,        # negative → None
            "last_verify_tests_total": "twenty",   # non-int → None
            "last_commit_hash": 12345,             # non-string → sanitize coerces
        },
    )
    summary = lss.LastSessionSummary(tmp_path)
    records = summary.load()
    r = records[0]
    assert r.last_apply_mode == "multi"   # good field survives
    assert r.last_apply_files is None     # bad int → None
    assert r.last_verify_tests_passed is None
    assert r.last_verify_tests_total is None
    # Render: apply requires files → omitted without files; verify missing both → omitted.
    prompt = summary.format_for_prompt() or ""
    assert "apply=" not in prompt
    assert "verify=" not in prompt


# ---------------------------------------------------------------------------
# (9) verify total=0 → verify= omitted (no div-by-zero signal)
# ---------------------------------------------------------------------------


def test_verify_total_zero_omits_token(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write(
        tmp_path, "bt-2026-04-15-170000",
        ops_digest={
            "last_verify_tests_passed": 0,
            "last_verify_tests_total": 0,
        },
    )
    summary = lss.LastSessionSummary(tmp_path)
    prompt = summary.format_for_prompt() or ""
    assert "verify=" not in prompt


# ---------------------------------------------------------------------------
# (10) Unknown last_apply_mode → treated as missing (defensive)
# ---------------------------------------------------------------------------


def test_unknown_apply_mode_rejected(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write(
        tmp_path, "bt-2026-04-15-180000",
        ops_digest={
            "last_apply_mode": "bogus_mode",   # not in the valid enum
            "last_apply_files": 3,
        },
    )
    summary = lss.LastSessionSummary(tmp_path)
    records = summary.load()
    assert records[0].last_apply_mode is None  # unknown → None
    prompt = summary.format_for_prompt() or ""
    assert "apply=" not in prompt


# ---------------------------------------------------------------------------
# SessionRecorder (implementer side) — observer round-trip
# ---------------------------------------------------------------------------


def test_recorder_records_apply_event_and_emits_schema_v2(tmp_path):
    rec = SessionRecorder("bt-2026-04-15-200000")
    rec.on_apply_succeeded(op_id="op-xyz", mode="multi", files=4)
    rec.on_verify_completed(
        op_id="op-xyz", passed=20, total=20, scoped_to_applied_op=True,
    )
    rec.on_commit_succeeded(
        op_id="op-xyz", commit_hash="0890a7b6f09123456789abcdef0123456789abcd",
    )
    path = rec.save_summary(
        output_dir=tmp_path,
        stop_reason="idle_timeout",
        duration_s=120.0,
        cost_total=0.0,
        cost_breakdown={},
        branch_stats={"commits": 0, "files_changed": 0, "insertions": 0, "deletions": 0},
        convergence_state="IMPROVING",
        convergence_slope=0.0,
        convergence_r2=0.0,
    )
    data = json.loads(path.read_text())
    assert data["schema_version"] == SCHEMA_VERSION
    digest = data["ops_digest"]
    assert digest["last_apply_mode"] == "multi"
    assert digest["last_apply_files"] == 4
    assert digest["last_apply_op_id"] == "op-xyz"
    assert digest["last_verify_tests_passed"] == 20
    assert digest["last_verify_tests_total"] == 20
    # Hash round-trip intact at full 40-char length.
    assert digest["last_commit_hash"] == "0890a7b6f09123456789abcdef0123456789abcd"


def test_recorder_omits_ops_digest_when_no_apply(tmp_path):
    rec = SessionRecorder("bt-2026-04-15-210000")
    # No on_apply_succeeded call.
    # Even with a stray verify/commit observation (shouldn't happen in
    # practice), the omit rule fires because _ops_digest_saw_apply
    # stays False.
    rec.on_verify_completed(
        op_id="op-abc", passed=1, total=1, scoped_to_applied_op=True,
    )
    path = rec.save_summary(
        output_dir=tmp_path,
        stop_reason="idle_timeout",
        duration_s=60.0,
        cost_total=0.0,
        cost_breakdown={},
        branch_stats={"commits": 0, "files_changed": 0, "insertions": 0, "deletions": 0},
        convergence_state="IMPROVING",
        convergence_slope=0.0,
        convergence_r2=0.0,
    )
    data = json.loads(path.read_text())
    assert "ops_digest" not in data  # OMITTED entirely
    assert data["schema_version"] == SCHEMA_VERSION  # still stamped


def test_recorder_most_recent_wins_overwrites_apply_mode(tmp_path):
    rec = SessionRecorder("bt-2026-04-15-220000")
    rec.on_apply_succeeded(op_id="op-1", mode="single", files=1)
    rec.on_apply_succeeded(op_id="op-2", mode="multi", files=5)
    digest = rec.ops_digest
    assert digest["last_apply_mode"] == "multi"
    assert digest["last_apply_files"] == 5
    assert digest["last_apply_op_id"] == "op-2"


def test_recorder_unscoped_verify_ignored(tmp_path):
    """Plan tightening #1: unscoped VERIFY (repo-wide) is dropped — no misleading 20/20."""
    rec = SessionRecorder("bt-2026-04-15-230000")
    rec.on_apply_succeeded(op_id="op-x", mode="single", files=1)
    rec.on_verify_completed(
        op_id="op-x", passed=999, total=1000, scoped_to_applied_op=False,
    )
    digest = rec.ops_digest
    assert "last_verify_tests_passed" not in digest
    assert "last_verify_tests_total" not in digest


def test_recorder_malformed_commit_hash_silently_dropped(tmp_path):
    rec = SessionRecorder("bt-2026-04-15-240000")
    rec.on_apply_succeeded(op_id="op-x", mode="single", files=1)
    rec.on_commit_succeeded(op_id="op-x", commit_hash="not-a-hash-at-all!!!")
    digest = rec.ops_digest
    assert "last_commit_hash" not in digest


def test_recorder_observer_calls_never_raise():
    """Belt-and-suspenders: pathological inputs don't propagate exceptions."""
    rec = SessionRecorder("bt-2026-04-15-250000")
    # Intentionally broken types — observer must swallow.
    rec.on_apply_succeeded(op_id=None, mode=None, files=None)  # type: ignore[arg-type]
    rec.on_verify_completed(
        op_id=None, passed=None, total=None, scoped_to_applied_op=True,  # type: ignore[arg-type]
    )
    rec.on_commit_succeeded(op_id=None, commit_hash=None)  # type: ignore[arg-type]
    # ops_digest stays empty since nothing valid was recorded.
    assert rec.ops_digest == {}


# ---------------------------------------------------------------------------
# OpsDigestObserver hook — register / noop default
# ---------------------------------------------------------------------------


def test_default_observer_is_noop():
    obs = odo.get_ops_digest_observer()
    # Default shouldn't raise on any call.
    obs.on_apply_succeeded(op_id="op-x", mode="multi", files=3)
    obs.on_verify_completed(op_id="op-x", passed=1, total=1)
    obs.on_commit_succeeded(op_id="op-x", commit_hash="abcd1234")


def test_register_and_reset_observer():
    rec = SessionRecorder("bt-reg-test")
    odo.register_ops_digest_observer(rec)
    assert odo.get_ops_digest_observer() is rec
    odo.reset_ops_digest_observer()
    assert odo.get_ops_digest_observer() is not rec


# ---------------------------------------------------------------------------
# Integration: round-trip through SessionRecorder + LastSessionSummary
# ---------------------------------------------------------------------------


def test_integration_recorder_to_summary_round_trip(monkeypatch, tmp_path):
    """Full v1.1a loop: record → write summary.json → LSS parses → renders."""
    _enable(monkeypatch)

    # Simulate a completed session: record events, write summary.json
    # to tmp_path at the session-dir the lex-max rule will find.
    session_dir = tmp_path / ".ouroboros" / "sessions" / "bt-2026-04-15-999999"
    session_dir.mkdir(parents=True)

    rec = SessionRecorder("bt-2026-04-15-999999")
    rec.on_apply_succeeded(op_id="op-integration", mode="multi", files=4)
    rec.on_verify_completed(
        op_id="op-integration", passed=18, total=20,
        scoped_to_applied_op=True,
    )
    rec.on_commit_succeeded(
        op_id="op-integration",
        commit_hash="0890a7b6f09876543210abcd",
    )
    rec.save_summary(
        output_dir=session_dir,
        stop_reason="budget_exhausted",
        duration_s=620.0,
        cost_total=0.45,
        cost_breakdown={"claude": 0.45},
        branch_stats={"commits": 1, "files_changed": 4, "insertions": 300, "deletions": 50},
        convergence_state="IMPROVING",
        convergence_slope=0.1,
        convergence_r2=0.8,
    )

    # Now LSS reads from that same tmp_path root.
    summary = lss.LastSessionSummary(tmp_path)
    records = summary.load()
    assert len(records) == 1
    r = records[0]
    assert r.last_apply_mode == "multi"
    assert r.last_apply_files == 4
    assert r.last_verify_tests_passed == 18
    assert r.last_verify_tests_total == 20
    assert r.last_commit_hash == "0890a7b6f09876543210abcd"

    prompt = summary.format_for_prompt() or ""
    assert "apply=multi/4" in prompt
    assert "verify=18/20" in prompt
    assert "commit=0890a7b6f0" in prompt
    assert "## Previous Session Closure" in prompt

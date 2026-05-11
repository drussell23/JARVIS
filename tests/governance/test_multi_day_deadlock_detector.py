"""Regression spine for §41.4 Phase 1 ninth (final) arc — Multi-Day Deadlock Detector."""
from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path

import pytest


from backend.core.ouroboros.governance import multi_day_deadlock_detector as mdd
from backend.core.ouroboros.governance.multi_day_deadlock_detector import (
    MULTI_DAY_DEADLOCK_SCHEMA_VERSION,
    DeadlockKind,
    DeadlockReport,
    DeadlockSeverity,
    DeadlockSignal,
    DeadlockVerdict,
    EvidenceSource,
    SessionDigest,
    _ENV_CRITICAL_SPAN_DAYS,
    _ENV_FAILURE_RATIO,
    _ENV_LEDGER_PATH,
    _ENV_LOOKBACK_DAYS,
    _ENV_MASTER,
    _ENV_MAX_SESSIONS,
    _ENV_MIN_OCCURRENCES,
    _ENV_PERSIST,
    _ENV_SESSION_ROOT,
    _ENV_THRASH_MIN_DAYS,
    _ENV_ZERO_PROGRESS_STREAK,
    _aggregate_verdict,
    _classify_severity,
    critical_span_days,
    detect_deadlocks,
    detect_repeat_failure,
    detect_repeat_stop_reason,
    detect_verdict_thrash,
    detect_zero_progress,
    failure_ratio_threshold,
    format_deadlock_panel,
    kind_glyph,
    ledger_path,
    lookback_days,
    master_enabled,
    max_sessions_scanned,
    min_occurrences,
    parse_session_summary,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    session_root,
    severity_glyph,
    source_glyph,
    thrash_min_days,
    verdict_glyph,
    walk_session_summaries,
    zero_progress_streak_threshold,
)


# Helpers


def _make_session(
    sid: str,
    *,
    age_days: float = 1.0,
    stop_reason: str = "complete",
    failure_ratio: float = 0.0,
    branch_commits: int = 1,
    stats_attempted: int = 10,
    stats_failed: int = 0,
) -> SessionDigest:
    if stats_failed == 0 and failure_ratio > 0:
        stats_failed = int(stats_attempted * failure_ratio)
    return SessionDigest(
        session_id=sid,
        age_days=age_days,
        stop_reason=stop_reason,
        session_outcome="complete",
        stats_attempted=stats_attempted,
        stats_failed=stats_failed,
        stats_completed=stats_attempted - stats_failed,
        branch_commits=branch_commits,
        last_apply_mode="single",
        failure_ratio=failure_ratio,
    )


def _write_summary(
    parent: Path, session_id: str,
    **fields,
) -> Path:
    sd = parent / session_id
    sd.mkdir(parents=True, exist_ok=True)
    summary_path = sd / "summary.json"
    base = {
        "session_id": session_id,
        "stop_reason": "complete",
        "stats": {"attempted": 5, "failed": 0, "completed": 5},
        "branch_stats": {"commits": 1},
    }
    base.update(fields)
    summary_path.write_text(json.dumps(base))
    return summary_path


# --- Schema + taxonomies ----------------------------------------------------


def test_schema_version_stamp():
    assert (
        MULTI_DAY_DEADLOCK_SCHEMA_VERSION
        == "multi_day_deadlock_detector.1"
    )


def test_deadlock_kind_closed():
    assert {v.value for v in DeadlockKind} == {
        "repeat_stop_reason", "repeat_failure",
        "verdict_thrash", "zero_progress",
    }


def test_deadlock_severity_closed():
    assert {v.value for v in DeadlockSeverity} == {
        "none", "low", "medium", "high",
    }


def test_evidence_source_closed():
    assert {v.value for v in EvidenceSource} == {
        "session_summary", "git_history",
        "ops_digest", "combined",
    }


def test_deadlock_verdict_closed():
    assert {v.value for v in DeadlockVerdict} == {
        "no_deadlock", "suspected", "confirmed", "disabled",
    }


# --- Env knobs -------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    assert master_enabled() is False


def test_master_enabled(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert master_enabled() is True


def test_persistence_default_true(monkeypatch):
    monkeypatch.delenv(_ENV_PERSIST, raising=False)
    assert persistence_enabled() is True


def test_lookback_days_default(monkeypatch):
    monkeypatch.delenv(_ENV_LOOKBACK_DAYS, raising=False)
    assert lookback_days() == 7


def test_lookback_clamp(monkeypatch):
    monkeypatch.setenv(_ENV_LOOKBACK_DAYS, "99999")
    assert lookback_days() == 365


def test_lookback_garbage(monkeypatch):
    monkeypatch.setenv(_ENV_LOOKBACK_DAYS, "abc")
    assert lookback_days() == 7


def test_min_occurrences_default(monkeypatch):
    monkeypatch.delenv(_ENV_MIN_OCCURRENCES, raising=False)
    assert min_occurrences() == 3


def test_min_occurrences_floor(monkeypatch):
    monkeypatch.setenv(_ENV_MIN_OCCURRENCES, "1")
    assert min_occurrences() == 2  # floor


def test_failure_ratio_default(monkeypatch):
    monkeypatch.delenv(_ENV_FAILURE_RATIO, raising=False)
    assert failure_ratio_threshold() == 0.5


def test_failure_ratio_clamp(monkeypatch):
    monkeypatch.setenv(_ENV_FAILURE_RATIO, "5.0")
    assert failure_ratio_threshold() == 1.0


def test_failure_ratio_garbage(monkeypatch):
    monkeypatch.setenv(_ENV_FAILURE_RATIO, "x")
    assert failure_ratio_threshold() == 0.5


def test_zero_progress_streak_default(monkeypatch):
    monkeypatch.delenv(_ENV_ZERO_PROGRESS_STREAK, raising=False)
    assert zero_progress_streak_threshold() == 3


def test_thrash_min_days_default(monkeypatch):
    monkeypatch.delenv(_ENV_THRASH_MIN_DAYS, raising=False)
    assert thrash_min_days() == 3


def test_critical_span_default(monkeypatch):
    monkeypatch.delenv(_ENV_CRITICAL_SPAN_DAYS, raising=False)
    assert critical_span_days() == 5


def test_max_sessions_default(monkeypatch):
    monkeypatch.delenv(_ENV_MAX_SESSIONS, raising=False)
    assert max_sessions_scanned() == 200


def test_session_root_default(monkeypatch):
    monkeypatch.delenv(_ENV_SESSION_ROOT, raising=False)
    assert ".ouroboros/sessions" in str(session_root())


def test_session_root_override(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_SESSION_ROOT, str(tmp_path))
    assert session_root() == tmp_path


def test_ledger_path_default(monkeypatch):
    monkeypatch.delenv(_ENV_LEDGER_PATH, raising=False)
    assert ".jarvis" in str(ledger_path())


def test_ledger_path_override(monkeypatch, tmp_path):
    custom = tmp_path / "d.jsonl"
    monkeypatch.setenv(_ENV_LEDGER_PATH, str(custom))
    assert ledger_path() == custom


# --- Glyphs ----------------------------------------------------------------


def test_kind_glyph_enum():
    assert kind_glyph(DeadlockKind.VERDICT_THRASH) == "⇄"


def test_kind_glyph_unknown():
    assert kind_glyph("bogus") == "?"


def test_severity_glyph_enum():
    assert severity_glyph(DeadlockSeverity.HIGH) == "█"


def test_severity_glyph_none():
    assert severity_glyph(None) == "?"


def test_source_glyph_enum():
    assert source_glyph(EvidenceSource.GIT_HISTORY) == "📜"


def test_source_glyph_str():
    assert source_glyph("ops_digest") == "📊"


def test_verdict_glyph_enum():
    assert verdict_glyph(DeadlockVerdict.CONFIRMED) == "!"


def test_verdict_glyph_disabled():
    assert verdict_glyph(DeadlockVerdict.DISABLED) == "◌"


# --- parse_session_summary -------------------------------------------------


def test_parse_summary_basic(tmp_path):
    p = _write_summary(
        tmp_path, "s1",
        stop_reason="wall_clock_cap",
        stats={"attempted": 10, "failed": 4, "completed": 6},
        branch_stats={"commits": 2},
    )
    digest = parse_session_summary(p)
    assert digest is not None
    assert digest.session_id == "s1"
    assert digest.stop_reason == "wall_clock_cap"
    assert digest.stats_attempted == 10
    assert digest.stats_failed == 4
    assert digest.branch_commits == 2
    assert abs(digest.failure_ratio - 0.4) < 0.01


def test_parse_summary_flat_fields(tmp_path):
    sd = tmp_path / "s1"
    sd.mkdir()
    (sd / "summary.json").write_text(json.dumps({
        "session_id": "s1",
        "stats_attempted": 5,
        "stats_failed": 1,
        "branch_commits": 1,
    }))
    digest = parse_session_summary(sd / "summary.json")
    assert digest is not None
    assert digest.stats_attempted == 5
    assert digest.stats_failed == 1
    assert digest.branch_commits == 1


def test_parse_summary_missing_file(tmp_path):
    assert parse_session_summary(tmp_path / "nope.json") is None


def test_parse_summary_malformed_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json at all")
    assert parse_session_summary(p) is None


def test_parse_summary_non_dict_json(tmp_path):
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]")
    assert parse_session_summary(p) is None


def test_parse_summary_zero_attempted(tmp_path):
    p = _write_summary(
        tmp_path, "s1",
        stats={"attempted": 0, "failed": 0, "completed": 0},
    )
    digest = parse_session_summary(p)
    assert digest is not None
    assert digest.failure_ratio == 0.0


def test_parse_summary_missing_keys_default_zero(tmp_path):
    sd = tmp_path / "s1"
    sd.mkdir()
    (sd / "summary.json").write_text("{}")
    digest = parse_session_summary(sd / "summary.json")
    assert digest is not None
    assert digest.stats_attempted == 0
    assert digest.stop_reason == ""


# --- walk_session_summaries ------------------------------------------------


def test_walk_summaries_nonexistent_root():
    assert walk_session_summaries(
        root=Path("/nonexistent_xyz_qwerty"),
    ) == ()


def test_walk_summaries_empty(tmp_path):
    assert walk_session_summaries(root=tmp_path) == ()


def test_walk_summaries_returns_digests(tmp_path):
    _write_summary(tmp_path, "s1")
    _write_summary(tmp_path, "s2")
    digests = walk_session_summaries(root=tmp_path)
    assert len(digests) == 2


def test_walk_summaries_filters_old(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_LOOKBACK_DAYS, "1")
    p1 = _write_summary(tmp_path, "fresh")
    p2 = _write_summary(tmp_path, "ancient")
    # Backdate one
    old = time.time() - 100 * 86400
    os.utime(p2, (old, old))
    os.utime(p2.parent, (old, old))
    digests = walk_session_summaries(root=tmp_path)
    ids = [d.session_id for d in digests]
    assert "fresh" in ids
    assert "ancient" not in ids


def test_walk_summaries_max_count(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_MAX_SESSIONS, "2")
    for i in range(5):
        _write_summary(tmp_path, f"s{i}")
    digests = walk_session_summaries(root=tmp_path)
    assert len(digests) == 2


def test_walk_summaries_skips_no_summary(tmp_path):
    (tmp_path / "incomplete").mkdir()
    _write_summary(tmp_path, "complete")
    digests = walk_session_summaries(root=tmp_path)
    assert len(digests) == 1


# --- _classify_severity ----------------------------------------------------


def test_severity_none_under_threshold():
    assert _classify_severity(
        1, 1.0, min_occ=3, critical_span=5,
    ) == DeadlockSeverity.NONE


def test_severity_low_at_threshold():
    assert _classify_severity(
        3, 1.0, min_occ=3, critical_span=5,
    ) == DeadlockSeverity.LOW


def test_severity_medium_at_2x():
    assert _classify_severity(
        6, 1.0, min_occ=3, critical_span=10,
    ) == DeadlockSeverity.MEDIUM


def test_severity_high_at_3x():
    assert _classify_severity(
        9, 1.0, min_occ=3, critical_span=10,
    ) == DeadlockSeverity.HIGH


def test_severity_high_via_critical_span():
    assert _classify_severity(
        3, 100.0, min_occ=3, critical_span=5,
    ) == DeadlockSeverity.HIGH


# --- detect_repeat_stop_reason ---------------------------------------------


def test_detect_repeat_stop_empty():
    assert detect_repeat_stop_reason(()) == ()


def test_detect_repeat_stop_clean_reasons_excluded():
    sessions = tuple(
        _make_session(f"s{i}", stop_reason="complete")
        for i in range(5)
    )
    # 'complete' is a clean reason — should not fire
    assert detect_repeat_stop_reason(sessions) == ()


def test_detect_repeat_stop_wall_clock_excluded():
    """wall_clock_cap is clean-bar-equivalent per CLAUDE.md."""
    sessions = tuple(
        _make_session(f"s{i}", stop_reason="wall_clock_cap")
        for i in range(5)
    )
    assert detect_repeat_stop_reason(sessions) == ()


def test_detect_repeat_stop_dirty_fires():
    sessions = tuple(
        _make_session(
            f"s{i}",
            age_days=float(i),
            stop_reason="incomplete_kill",
        )
        for i in range(5)
    )
    signals = detect_repeat_stop_reason(sessions)
    assert len(signals) == 1
    assert signals[0].kind == DeadlockKind.REPEAT_STOP_REASON
    assert signals[0].evidence_source == EvidenceSource.SESSION_SUMMARY
    assert signals[0].occurrences == 5


def test_detect_repeat_stop_below_threshold_silent():
    sessions = tuple(
        _make_session(f"s{i}", stop_reason="hung")
        for i in range(2)  # below default min_occ=3
    )
    assert detect_repeat_stop_reason(sessions) == ()


def test_detect_repeat_stop_multiple_reasons():
    sessions = (
        _make_session("a1", stop_reason="hung"),
        _make_session("a2", stop_reason="hung"),
        _make_session("a3", stop_reason="hung"),
        _make_session("b1", stop_reason="oom"),
        _make_session("b2", stop_reason="oom"),
        _make_session("b3", stop_reason="oom"),
    )
    signals = detect_repeat_stop_reason(sessions)
    assert len(signals) == 2


def test_detect_repeat_stop_severity_escalates():
    # 9 sessions = 3× min_occ → HIGH
    sessions = tuple(
        _make_session(f"s{i}", stop_reason="hung")
        for i in range(9)
    )
    signals = detect_repeat_stop_reason(sessions)
    assert len(signals) == 1
    assert signals[0].severity == DeadlockSeverity.HIGH


# --- detect_repeat_failure -------------------------------------------------


def test_detect_repeat_failure_empty():
    assert detect_repeat_failure(()) == ()


def test_detect_repeat_failure_low_ratio_silent():
    sessions = tuple(
        _make_session(
            f"s{i}", failure_ratio=0.1,
            stats_attempted=10, stats_failed=1,
        )
        for i in range(5)
    )
    assert detect_repeat_failure(sessions) == ()


def test_detect_repeat_failure_high_ratio_fires():
    sessions = tuple(
        _make_session(
            f"s{i}", failure_ratio=0.8,
            stats_attempted=10, stats_failed=8,
        )
        for i in range(5)
    )
    signals = detect_repeat_failure(sessions)
    assert len(signals) == 1
    assert signals[0].kind == DeadlockKind.REPEAT_FAILURE


def test_detect_repeat_failure_below_count_silent():
    sessions = tuple(
        _make_session(
            f"s{i}", failure_ratio=0.9,
            stats_attempted=10, stats_failed=9,
        )
        for i in range(2)  # below min_occ=3
    )
    assert detect_repeat_failure(sessions) == ()


def test_detect_repeat_failure_zero_attempted_excluded():
    sessions = tuple(
        _make_session(
            f"s{i}", failure_ratio=0.0,
            stats_attempted=0, stats_failed=0,
        )
        for i in range(5)
    )
    assert detect_repeat_failure(sessions) == ()


# --- detect_zero_progress --------------------------------------------------


def test_detect_zero_progress_empty():
    assert detect_zero_progress(()) == ()


def test_detect_zero_progress_streak_fires():
    sessions = tuple(
        _make_session(
            f"s{i}", age_days=float(i), branch_commits=0,
        )
        for i in range(5)
    )
    signals = detect_zero_progress(sessions)
    assert len(signals) == 1
    assert signals[0].kind == DeadlockKind.ZERO_PROGRESS


def test_detect_zero_progress_broken_streak_silent():
    # Streak starts but breaks on second-newest
    sessions = (
        _make_session("newest", age_days=0.0, branch_commits=0),
        _make_session("middle", age_days=1.0, branch_commits=3),
        _make_session("old", age_days=2.0, branch_commits=0),
    )
    # Only 1 zero from newest before streak breaks — below threshold
    assert detect_zero_progress(sessions) == ()


def test_detect_zero_progress_short_streak_silent():
    sessions = tuple(
        _make_session(
            f"s{i}", age_days=float(i), branch_commits=0,
        )
        for i in range(2)  # below threshold=3
    )
    assert detect_zero_progress(sessions) == ()


def test_detect_zero_progress_severity_escalates():
    sessions = tuple(
        _make_session(
            f"s{i}", age_days=float(i), branch_commits=0,
        )
        for i in range(9)  # 3× threshold
    )
    signals = detect_zero_progress(sessions)
    assert len(signals) == 1
    assert signals[0].severity == DeadlockSeverity.HIGH


# --- detect_verdict_thrash -------------------------------------------------


_SEP = "<<<JARVIS_COMMIT_SEP>>>"
_FSEP = "<<<JARVIS_FIELD_SEP>>>"


def _make_git_log(commits):
    out = []
    for c in commits:
        files = "\n".join(c.get("files", []))
        if files:
            out.append(
                f"{c['sha']}{_FSEP}{c['subject']}{_FSEP}{c['author']}{_FSEP}{int(c['epoch'])}\n"
                f"{files}{_SEP}"
            )
        else:
            out.append(
                f"{c['sha']}{_FSEP}{c['subject']}{_FSEP}{c['author']}{_FSEP}{int(c['epoch'])}{_SEP}"
            )
    return "\n".join(out)


def test_detect_thrash_no_commits():
    def empty_runner(args):
        return ""
    assert detect_verdict_thrash(git_runner=empty_runner) == ()


def test_detect_thrash_below_threshold_silent():
    now = time.time()
    # 2 bugfix days — below default threshold=3
    log = _make_git_log([
        {"sha": "a", "subject": "fix: x", "author": "A",
         "epoch": now - 86400, "files": ["hot.py"]},
        {"sha": "b", "subject": "fix: y", "author": "A",
         "epoch": now - 2 * 86400, "files": ["hot.py"]},
    ])
    def runner(args):
        return log
    assert detect_verdict_thrash(
        git_runner=runner, now_unix=now,
    ) == ()


def test_detect_thrash_fires_on_repeated_bugfix():
    now = time.time()
    # 4 distinct days, all bugfix on hot.py
    log = _make_git_log([
        {"sha": f"s{i}", "subject": f"fix: bug {i}",
         "author": "A", "epoch": now - i * 86400 * 1.5,
         "files": ["hot.py"]}
        for i in range(4)
    ])
    def runner(args):
        return log
    signals = detect_verdict_thrash(
        git_runner=runner, now_unix=now,
    )
    assert len(signals) == 1
    assert signals[0].kind == DeadlockKind.VERDICT_THRASH
    assert "hot.py" in signals[0].affected_files


def test_detect_thrash_feature_commits_ignored():
    now = time.time()
    # 3 feature commits — not bugfix → no thrash
    log = _make_git_log([
        {"sha": f"s{i}", "subject": f"feat: thing {i}",
         "author": "A", "epoch": now - i * 86400,
         "files": ["x.py"]}
        for i in range(3)
    ])
    def runner(args):
        return log
    assert detect_verdict_thrash(
        git_runner=runner, now_unix=now,
    ) == ()


def test_detect_thrash_multiple_files():
    now = time.time()
    log = _make_git_log([
        {"sha": f"s{i}", "subject": f"fix: bug {i}",
         "author": "A", "epoch": now - i * 86400 * 1.5,
         "files": ["a.py", "b.py"]}
        for i in range(4)
    ])
    def runner(args):
        return log
    signals = detect_verdict_thrash(
        git_runner=runner, now_unix=now,
    )
    # Both files thrash
    affected = []
    for s in signals:
        affected.extend(s.affected_files)
    assert "a.py" in affected
    assert "b.py" in affected


# --- _aggregate_verdict ----------------------------------------------------


def test_aggregate_verdict_empty():
    assert _aggregate_verdict(()) == DeadlockVerdict.NO_DEADLOCK


def test_aggregate_verdict_low_only():
    signals = (DeadlockSignal(
        kind=DeadlockKind.REPEAT_FAILURE,
        severity=DeadlockSeverity.LOW,
        fingerprint="x", evidence_source=EvidenceSource.SESSION_SUMMARY,
        evidence_text="", occurrences=3, span_days=1.0,
        affected_files=(), boundary_crossed=False,
    ),)
    assert _aggregate_verdict(signals) == DeadlockVerdict.SUSPECTED


def test_aggregate_verdict_high_confirmed():
    signals = (DeadlockSignal(
        kind=DeadlockKind.REPEAT_FAILURE,
        severity=DeadlockSeverity.HIGH,
        fingerprint="x", evidence_source=EvidenceSource.SESSION_SUMMARY,
        evidence_text="", occurrences=9, span_days=5.0,
        affected_files=(), boundary_crossed=False,
    ),)
    assert _aggregate_verdict(signals) == DeadlockVerdict.CONFIRMED


# --- detect_deadlocks (top-level) ------------------------------------------


def test_top_level_master_off(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "false")
    report = detect_deadlocks()
    assert report.verdict == DeadlockVerdict.DISABLED
    assert report.master_enabled is False


def test_top_level_master_on_no_sessions(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = detect_deadlocks(sessions_override=())
    assert report.verdict == DeadlockVerdict.NO_DEADLOCK
    assert report.master_enabled is True


def test_top_level_detects_all_kinds(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    sessions = tuple(
        SessionDigest(
            session_id=f"s{i}",
            age_days=float(i),
            stop_reason="incomplete_kill",
            session_outcome="incomplete",
            stats_attempted=10, stats_failed=8, stats_completed=2,
            branch_commits=0,
            last_apply_mode="none",
            failure_ratio=0.8,
        )
        for i in range(5)
    )
    report = detect_deadlocks(sessions_override=sessions)
    kinds = {s.kind for s in report.signals}
    assert DeadlockKind.REPEAT_STOP_REASON in kinds
    assert DeadlockKind.REPEAT_FAILURE in kinds
    assert DeadlockKind.ZERO_PROGRESS in kinds


def test_top_level_elapsed_recorded(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = detect_deadlocks(sessions_override=())
    assert report.elapsed_s >= 0.0


# --- Serialization ---------------------------------------------------------


def test_session_digest_to_dict():
    d = _make_session("s1").to_dict()
    assert d["session_id"] == "s1"
    assert d["schema_version"] == "multi_day_deadlock_detector.1"


def test_deadlock_signal_to_dict():
    sig = DeadlockSignal(
        kind=DeadlockKind.VERDICT_THRASH,
        severity=DeadlockSeverity.MEDIUM,
        fingerprint="x",
        evidence_source=EvidenceSource.GIT_HISTORY,
        evidence_text="ev",
        occurrences=5,
        span_days=3.0,
        affected_files=("a.py",),
        boundary_crossed=False,
    )
    d = sig.to_dict()
    assert d["kind"] == "verdict_thrash"
    assert d["severity"] == "medium"
    assert d["affected_files"] == ["a.py"]


def test_deadlock_report_to_dict():
    report = DeadlockReport(
        evaluated_at_unix=1.0,
        master_enabled=True,
        verdict=DeadlockVerdict.SUSPECTED,
        lookback_days=7,
        sessions_scanned=5,
        signals=(),
        diagnostic="x",
        elapsed_s=0.1,
    )
    d = report.to_dict()
    assert d["verdict"] == "suspected"
    assert d["lookback_days"] == 7


# --- Persistence -----------------------------------------------------------


def test_persistence_disabled_no_write(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv(_ENV_LEDGER_PATH, str(ledger))
    detect_deadlocks(sessions_override=())
    assert not ledger.exists()


# --- Panel rendering --------------------------------------------------------


def test_format_panel_master_off(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "false")
    text = format_deadlock_panel(None)
    assert "disabled" in text


def test_format_panel_no_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    text = format_deadlock_panel(None)
    assert "no report" in text


def test_format_panel_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    sessions = tuple(
        _make_session(
            f"s{i}", stop_reason="hung", age_days=float(i),
        )
        for i in range(5)
    )
    report = detect_deadlocks(sessions_override=sessions)
    text = format_deadlock_panel(report)
    assert "Multi-Day Deadlock" in text


# --- SSE event registration ------------------------------------------------


def test_sse_event_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_MULTI_DAY_DEADLOCK_EVALUATED,
        _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_MULTI_DAY_DEADLOCK_EVALUATED == (
        "multi_day_deadlock_evaluated"
    )
    assert EVENT_TYPE_MULTI_DAY_DEADLOCK_EVALUATED in _VALID_EVENT_TYPES


# --- FlagRegistry seeds ----------------------------------------------------


def test_register_flags_count():
    class FakeRegistry:
        def __init__(self):
            self.registered = []

        def register(self, spec):
            self.registered.append(spec)

    reg = FakeRegistry()
    count = register_flags(reg)
    assert count >= 10
    names = [s.name for s in reg.registered]
    assert _ENV_MASTER in names
    assert _ENV_FAILURE_RATIO in names


def test_register_flags_master_default_false():
    class FakeRegistry:
        def __init__(self):
            self.registered = []

        def register(self, spec):
            self.registered.append(spec)

    reg = FakeRegistry()
    register_flags(reg)
    master_specs = [s for s in reg.registered if s.name == _ENV_MASTER]
    assert master_specs
    assert master_specs[0].default is False


# --- AST pins ---------------------------------------------------------------


def _load_source_tree():
    target = Path(
        "backend/core/ouroboros/governance/"
        "multi_day_deadlock_detector.py"
    )
    src = target.read_text()
    return src, ast.parse(src)


def test_ast_pins_count():
    pins = register_shipped_invariants()
    assert len(pins) == 7


def test_ast_pin_kind_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "kind_taxonomy" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_severity_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "severity_taxonomy" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_source_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "source_taxonomy" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_verdict_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "verdict_taxonomy" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_authority_asymmetry_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_master_default_false_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_composes_canonical_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


# --- AST pin synthetic regressions -----------------------------------------


def test_ast_pin_kind_taxonomy_catches_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "kind_taxonomy" in p.invariant_name
    )
    bad = '''
class DeadlockKind(str, enum.Enum):
    REPEAT_STOP_REASON = "repeat_stop_reason"
    REPEAT_FAILURE = "repeat_failure"
    VERDICT_THRASH = "verdict_thrash"
    BOGUS_NEW = "bogus_new"
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_severity_catches_missing():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "severity_taxonomy" in p.invariant_name
    )
    bad = '''
class DeadlockSeverity(str, enum.Enum):
    NONE = "none"
    LOW = "low"
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_source_catches_typo():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "source_taxonomy" in p.invariant_name
    )
    bad = '''
class EvidenceSource(str, enum.Enum):
    SESSION_SUMMARY = "sesion_summary"
    GIT_HISTORY = "git_history"
    OPS_DIGEST = "ops_digest"
    COMBINED = "combined"
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_verdict_catches_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "verdict_taxonomy" in p.invariant_name
    )
    bad = '''
class DeadlockVerdict(str, enum.Enum):
    NO_DEADLOCK = "no_deadlock"
    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"
    EXTRA = "extra"
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_authority_catches_forbidden_import():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = '''
from backend.core.ouroboros.governance.semantic_guardian import x
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_master_default_false_catches_true():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad = '''
def master_enabled():
    return _flag("X", default=True)
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_composes_canonical_catches_missing():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    # Missing last_session_summary reference
    bad = '''
# long_horizon_memory
# governance_boundary_gate
# cross_process_jsonl
import subprocess
import pathlib
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()

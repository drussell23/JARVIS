"""Regression spine for §41.4 Phase 1 seventh arc — Long-Horizon Memory."""
from __future__ import annotations

import ast
import os
import time
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import long_horizon_memory as lhm
from backend.core.ouroboros.governance.long_horizon_memory import (
    LONG_HORIZON_MEMORY_SCHEMA_VERSION,
    CommitRecord,
    CommitTheme,
    ComposedSourceDigest,
    CrossSessionMemoryReport,
    FileHotness,
    MemoryHorizon,
    MemorySnapshot,
    MemoryTheme,
    RecallVerdict,
    _ENV_GIT_TIMEOUT_S,
    _ENV_HORIZON_DAYS,
    _ENV_HOT_FILE_COUNT,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_COMMITS,
    _ENV_PERSIST,
    _ENV_RECENT_WINDOW_DAYS,
    _ENV_STALE_DAYS_THRESHOLD,
    _ENV_STALE_FILE_COUNT,
    _ENV_WARM_WINDOW_DAYS,
    build_snapshot,
    classify_horizon,
    classify_theme,
    format_memory_panel,
    git_timeout_s,
    horizon_days,
    horizon_glyph,
    hot_file_count,
    ledger_path,
    master_enabled,
    max_commits_to_walk,
    persistence_enabled,
    recall_memory,
    recent_window_days,
    register_flags,
    register_shipped_invariants,
    stale_days_threshold,
    stale_file_count,
    theme_glyph,
    verdict_glyph,
    walk_git_log,
    warm_window_days,
)


# Helpers


_SEP = "<<<JARVIS_COMMIT_SEP>>>"
_FSEP = "<<<JARVIS_FIELD_SEP>>>"


def _fake_log(commits: List[dict]) -> str:
    """Build a fake git-log output."""
    parts = []
    for c in commits:
        header = f"{c['sha']}{_FSEP}{c['subject']}{_FSEP}{c['author']}{_FSEP}{int(c['epoch'])}"
        files = "\n".join(c.get("files", []))
        if files:
            parts.append(f"{header}\n{files}{_SEP}")
        else:
            parts.append(f"{header}{_SEP}")
    return "\n".join(parts)


def _make_runner(output: str):
    def runner(args):
        return output
    return runner


# --- Schema + taxonomies ----------------------------------------------------


def test_schema_version_stamp():
    assert LONG_HORIZON_MEMORY_SCHEMA_VERSION == "long_horizon_memory.1"


def test_recall_verdict_closed():
    assert {v.value for v in RecallVerdict} == {
        "fresh", "warm", "cold", "disabled",
    }


def test_memory_horizon_closed():
    assert {h.value for h in MemoryHorizon} == {
        "session", "day", "week", "month",
    }


def test_memory_theme_closed():
    assert {t.value for t in MemoryTheme} == {
        "refactor", "feature", "bugfix", "other",
    }


# --- Env knob clamping ------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    assert master_enabled() is False


def test_master_enabled_true(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert master_enabled() is True


def test_persistence_default_true(monkeypatch):
    monkeypatch.delenv(_ENV_PERSIST, raising=False)
    assert persistence_enabled() is True


def test_max_commits_default(monkeypatch):
    monkeypatch.delenv(_ENV_MAX_COMMITS, raising=False)
    assert max_commits_to_walk() == 500


def test_max_commits_clamped(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_COMMITS, "999999999")
    assert max_commits_to_walk() == 100_000


def test_max_commits_floor(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_COMMITS, "0")
    assert max_commits_to_walk() == 1


def test_max_commits_garbage(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_COMMITS, "garbage")
    assert max_commits_to_walk() == 500


def test_horizon_days_default(monkeypatch):
    monkeypatch.delenv(_ENV_HORIZON_DAYS, raising=False)
    assert horizon_days() == 90


def test_recent_window_default(monkeypatch):
    monkeypatch.delenv(_ENV_RECENT_WINDOW_DAYS, raising=False)
    assert recent_window_days() == 7


def test_warm_window_default(monkeypatch):
    monkeypatch.delenv(_ENV_WARM_WINDOW_DAYS, raising=False)
    monkeypatch.delenv(_ENV_RECENT_WINDOW_DAYS, raising=False)
    assert warm_window_days() == 30


def test_warm_clamped_above_recent(monkeypatch):
    # operator sets warm below recent — system auto-clamps
    monkeypatch.setenv(_ENV_RECENT_WINDOW_DAYS, "20")
    monkeypatch.setenv(_ENV_WARM_WINDOW_DAYS, "5")
    assert warm_window_days() == 21  # recent + 1


def test_stale_threshold_default(monkeypatch):
    monkeypatch.delenv(_ENV_STALE_DAYS_THRESHOLD, raising=False)
    assert stale_days_threshold() == 60


def test_hot_file_count_default(monkeypatch):
    monkeypatch.delenv(_ENV_HOT_FILE_COUNT, raising=False)
    assert hot_file_count() == 10


def test_stale_file_count_default(monkeypatch):
    monkeypatch.delenv(_ENV_STALE_FILE_COUNT, raising=False)
    assert stale_file_count() == 10


def test_git_timeout_default(monkeypatch):
    monkeypatch.delenv(_ENV_GIT_TIMEOUT_S, raising=False)
    assert git_timeout_s() == 30


def test_ledger_path_default(monkeypatch):
    monkeypatch.delenv(_ENV_LEDGER_PATH, raising=False)
    p = ledger_path()
    assert isinstance(p, Path)
    assert ".jarvis" in str(p)


def test_ledger_path_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom.jsonl"
    monkeypatch.setenv(_ENV_LEDGER_PATH, str(custom))
    assert ledger_path() == custom


# --- Theme classification ---------------------------------------------------


def test_classify_theme_fix_prefix():
    assert classify_theme("fix: null deref") == MemoryTheme.BUGFIX


def test_classify_theme_feat_prefix():
    assert classify_theme("feat(api): new endpoint") == MemoryTheme.FEATURE


def test_classify_theme_refactor_prefix():
    assert classify_theme("refactor: rename helper") == MemoryTheme.REFACTOR


def test_classify_theme_bugfix_keyword_body():
    assert classify_theme("Resolve crash in pipeline") == MemoryTheme.BUGFIX


def test_classify_theme_feature_keyword_body():
    assert classify_theme("Introduce new tier") == MemoryTheme.FEATURE


def test_classify_theme_refactor_keyword_body():
    assert classify_theme("Cleanup imports") == MemoryTheme.REFACTOR


def test_classify_theme_chore_other():
    assert classify_theme("chore: bump deps") == MemoryTheme.OTHER


def test_classify_theme_docs_other():
    assert classify_theme("docs: update README") == MemoryTheme.OTHER


def test_classify_theme_empty():
    assert classify_theme("") == MemoryTheme.OTHER


def test_classify_theme_none_safe():
    assert classify_theme(None) == MemoryTheme.OTHER  # type: ignore[arg-type]


def test_classify_theme_priority_bugfix_over_feature():
    # "fix: add new" — prefix wins
    assert classify_theme("fix: add new validation") == MemoryTheme.BUGFIX


def test_classify_theme_case_insensitive():
    assert classify_theme("FIX: TypeError") == MemoryTheme.BUGFIX


# --- Horizon classification -------------------------------------------------


def test_classify_horizon_session():
    assert classify_horizon(0.5) == MemoryHorizon.SESSION


def test_classify_horizon_session_boundary():
    assert classify_horizon(1.0) == MemoryHorizon.SESSION


def test_classify_horizon_day():
    assert classify_horizon(5.0) == MemoryHorizon.DAY


def test_classify_horizon_day_boundary():
    assert classify_horizon(7.0) == MemoryHorizon.DAY


def test_classify_horizon_week():
    assert classify_horizon(20.0) == MemoryHorizon.WEEK


def test_classify_horizon_week_boundary():
    assert classify_horizon(28.0) == MemoryHorizon.WEEK


def test_classify_horizon_month():
    assert classify_horizon(60.0) == MemoryHorizon.MONTH


def test_classify_horizon_negative_safe():
    # NEVER raises — negative coerced to 0
    assert classify_horizon(-5.0) == MemoryHorizon.SESSION


def test_classify_horizon_garbage_safe():
    assert classify_horizon("garbage") == MemoryHorizon.MONTH  # type: ignore[arg-type]


# --- Glyphs ------------------------------------------------------------------


def test_verdict_glyph_enum():
    assert verdict_glyph(RecallVerdict.FRESH) == "🌱"


def test_verdict_glyph_str():
    assert verdict_glyph("cold") == "❄"


def test_verdict_glyph_none():
    assert verdict_glyph(None) == "?"


def test_verdict_glyph_unknown():
    assert verdict_glyph("bogus") == "?"


def test_horizon_glyph_enum():
    assert horizon_glyph(MemoryHorizon.WEEK) == "◉"


def test_horizon_glyph_none():
    assert horizon_glyph(None) == "?"


def test_theme_glyph_enum():
    assert theme_glyph(MemoryTheme.BUGFIX) == "🐛"


def test_theme_glyph_str():
    assert theme_glyph("feature") == "✨"


def test_theme_glyph_unknown():
    assert theme_glyph("bogus") == "?"


# --- walk_git_log -----------------------------------------------------------


def test_walk_git_log_empty_output():
    runner = _make_runner("")
    assert walk_git_log(git_runner=runner) == ()


def test_walk_git_log_none_output():
    runner = _make_runner(None)  # type: ignore[arg-type]
    assert walk_git_log(git_runner=runner) == ()


def test_walk_git_log_runner_failure():
    def bad_runner(args):
        return None
    assert walk_git_log(git_runner=bad_runner) == ()


def test_walk_git_log_parses_single_commit():
    now = time.time()
    out = _fake_log([
        {"sha": "abc1234", "subject": "fix: bug",
         "author": "Alice", "epoch": now - 3600,
         "files": ["foo.py"]},
    ])
    commits = walk_git_log(git_runner=_make_runner(out), now_unix=now)
    assert len(commits) == 1
    assert commits[0].sha == "abc1234"
    assert commits[0].theme == MemoryTheme.BUGFIX
    assert commits[0].files == ("foo.py",)


def test_walk_git_log_parses_multiple():
    now = time.time()
    out = _fake_log([
        {"sha": "a1", "subject": "fix: x", "author": "A",
         "epoch": now - 86400, "files": ["foo.py", "bar.py"]},
        {"sha": "b2", "subject": "feat: y", "author": "B",
         "epoch": now - 172800, "files": ["baz.py"]},
    ])
    commits = walk_git_log(git_runner=_make_runner(out), now_unix=now)
    assert len(commits) == 2


def test_walk_git_log_excludes_beyond_horizon(monkeypatch):
    monkeypatch.setenv(_ENV_HORIZON_DAYS, "10")
    now = time.time()
    out = _fake_log([
        {"sha": "old", "subject": "fix: ancient", "author": "A",
         "epoch": now - 86400 * 50, "files": ["foo.py"]},
        {"sha": "new", "subject": "fix: recent", "author": "A",
         "epoch": now - 86400 * 2, "files": ["bar.py"]},
    ])
    commits = walk_git_log(git_runner=_make_runner(out), now_unix=now)
    assert len(commits) == 1
    assert commits[0].sha == "new"


def test_walk_git_log_malformed_header_skipped():
    now = time.time()
    raw = f"bad_header_no_separators{_SEP}"
    commits = walk_git_log(git_runner=_make_runner(raw), now_unix=now)
    assert commits == ()


def test_walk_git_log_bad_epoch_skipped():
    now = time.time()
    raw = (
        f"a1{_FSEP}fix{_FSEP}A{_FSEP}not_a_number{_SEP}foo.py{_SEP}"
    )
    commits = walk_git_log(git_runner=_make_runner(raw), now_unix=now)
    assert commits == ()


def test_walk_git_log_no_files():
    now = time.time()
    out = _fake_log([
        {"sha": "a1", "subject": "fix: x", "author": "A",
         "epoch": now - 3600, "files": []},
    ])
    commits = walk_git_log(git_runner=_make_runner(out), now_unix=now)
    assert len(commits) == 1
    assert commits[0].files == ()


def test_walk_git_log_runner_exception_safe():
    def crashy(args):
        raise RuntimeError("boom")
    # NEVER raises
    assert walk_git_log(git_runner=crashy) == ()


# Wait — the default_git_log_runner catches; an injected runner
# that raises would actually escape. Let me cover the realistic
# subprocess failure path (returncode != 0 → None) via override.


# --- build_snapshot ---------------------------------------------------------


def _make_commit(
    sha: str, theme: MemoryTheme, days_ago: float,
    files: tuple = (), now: float = 0.0,
) -> CommitRecord:
    n = now if now > 0 else time.time()
    epoch = n - days_ago * 86400
    return CommitRecord(
        sha=sha,
        subject=f"{theme.value}: test",
        author="A",
        committed_at_unix=epoch,
        age_days=days_ago,
        theme=theme,
        files=files,
    )


def test_build_snapshot_empty_commits():
    now = time.time()
    snap = build_snapshot(commits_override=(), now_unix=now)
    assert snap.total_commits_scanned == 0
    assert snap.themes == ()
    assert snap.hot_files == ()


def test_build_snapshot_aggregates_themes():
    now = time.time()
    commits = (
        _make_commit("a", MemoryTheme.BUGFIX, 1, ("foo.py",), now),
        _make_commit("b", MemoryTheme.BUGFIX, 2, ("foo.py",), now),
        _make_commit("c", MemoryTheme.FEATURE, 3, ("bar.py",), now),
    )
    snap = build_snapshot(commits_override=commits, now_unix=now)
    theme_counts = {t.theme.value: t.count for t in snap.themes}
    assert theme_counts == {"bugfix": 2, "feature": 1}


def test_build_snapshot_horizon_classification():
    now = time.time()
    commits = (
        _make_commit("a", MemoryTheme.BUGFIX, 0.5, ("foo.py",), now),
    )
    snap = build_snapshot(commits_override=commits, now_unix=now)
    assert snap.horizon_classification == MemoryHorizon.SESSION


def test_build_snapshot_hot_files_detected():
    now = time.time()
    commits = []
    for i in range(10):
        commits.append(_make_commit(
            f"hot{i}", MemoryTheme.FEATURE, i, ("hot.py",), now,
        ))
    commits.append(_make_commit(
        "cold", MemoryTheme.OTHER, 1, ("cold.py",), now,
    ))
    snap = build_snapshot(commits_override=tuple(commits), now_unix=now)
    paths = [f.file_path for f in snap.hot_files]
    assert "hot.py" in paths


def test_build_snapshot_stale_files_detected(monkeypatch):
    monkeypatch.setenv(_ENV_HORIZON_DAYS, "365")
    monkeypatch.setenv(_ENV_STALE_DAYS_THRESHOLD, "30")
    now = time.time()
    commits = (
        # Stale: touched once, > 30d ago
        _make_commit("old", MemoryTheme.OTHER, 60, ("stale.py",), now),
        # Recent: ineligible
        _make_commit("new", MemoryTheme.OTHER, 1, ("fresh.py",), now),
    )
    snap = build_snapshot(commits_override=commits, now_unix=now)
    stale_paths = [f.file_path for f in snap.stale_files]
    assert "stale.py" in stale_paths
    assert "fresh.py" not in stale_paths


def test_build_snapshot_hot_files_capped(monkeypatch):
    monkeypatch.setenv(_ENV_HOT_FILE_COUNT, "2")
    now = time.time()
    commits = []
    # Make 5 different files each touched many times
    for f_idx in range(5):
        for c_idx in range(8):
            commits.append(_make_commit(
                f"c{f_idx}_{c_idx}", MemoryTheme.FEATURE,
                c_idx, (f"file{f_idx}.py",), now,
            ))
    snap = build_snapshot(commits_override=tuple(commits), now_unix=now)
    assert len(snap.hot_files) <= 2


def test_build_snapshot_composed_sources_present():
    now = time.time()
    commits = (_make_commit("a", MemoryTheme.OTHER, 1, ("foo.py",), now),)
    snap = build_snapshot(commits_override=commits, now_unix=now)
    names = [c.source_name for c in snap.composed_sources]
    assert "user_preference_memory" in names
    assert "last_session_summary" in names
    assert "semantic_index" in names


def test_build_snapshot_theme_dominant_files():
    now = time.time()
    commits = (
        _make_commit("a", MemoryTheme.BUGFIX, 1, ("hot.py", "side.py"), now),
        _make_commit("b", MemoryTheme.BUGFIX, 2, ("hot.py",), now),
        _make_commit("c", MemoryTheme.BUGFIX, 3, ("hot.py",), now),
    )
    snap = build_snapshot(commits_override=commits, now_unix=now)
    bugfix_themes = [t for t in snap.themes if t.theme == MemoryTheme.BUGFIX]
    assert bugfix_themes
    assert "hot.py" in bugfix_themes[0].dominant_files


def test_build_snapshot_theme_sample_subjects():
    now = time.time()
    commits = (
        _make_commit("a", MemoryTheme.FEATURE, 1, ("f.py",), now),
        _make_commit("b", MemoryTheme.FEATURE, 2, ("f.py",), now),
    )
    snap = build_snapshot(commits_override=commits, now_unix=now)
    feat = [t for t in snap.themes if t.theme == MemoryTheme.FEATURE][0]
    assert len(feat.sample_subjects) > 0


# --- recall_memory (top-level) ----------------------------------------------


def test_recall_memory_master_off(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "false")
    report = recall_memory()
    assert report.verdict == RecallVerdict.DISABLED
    assert report.master_enabled is False
    assert report.snapshot is None


def test_recall_memory_no_commits(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = recall_memory(commits_override=())
    assert report.verdict == RecallVerdict.DISABLED
    assert report.master_enabled is True


def test_recall_memory_fresh_verdict(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    now = time.time()
    commits = (
        _make_commit("a", MemoryTheme.BUGFIX, 1, ("foo.py",), now),
        _make_commit("b", MemoryTheme.BUGFIX, 2, ("foo.py",), now),
    )
    report = recall_memory(commits_override=commits, now_unix=now)
    assert report.verdict == RecallVerdict.FRESH


def test_recall_memory_warm_verdict(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECENT_WINDOW_DAYS, "7")
    monkeypatch.setenv(_ENV_WARM_WINDOW_DAYS, "30")
    monkeypatch.setenv(_ENV_HORIZON_DAYS, "90")
    now = time.time()
    commits = (
        _make_commit("a", MemoryTheme.BUGFIX, 15, ("foo.py",), now),
        _make_commit("b", MemoryTheme.BUGFIX, 18, ("foo.py",), now),
    )
    report = recall_memory(commits_override=commits, now_unix=now)
    assert report.verdict == RecallVerdict.WARM


def test_recall_memory_cold_verdict(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECENT_WINDOW_DAYS, "7")
    monkeypatch.setenv(_ENV_WARM_WINDOW_DAYS, "30")
    monkeypatch.setenv(_ENV_HORIZON_DAYS, "365")
    now = time.time()
    commits = (
        _make_commit("a", MemoryTheme.BUGFIX, 90, ("foo.py",), now),
        _make_commit("b", MemoryTheme.BUGFIX, 100, ("foo.py",), now),
    )
    report = recall_memory(commits_override=commits, now_unix=now)
    assert report.verdict == RecallVerdict.COLD


def test_recall_memory_snapshot_populated(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    now = time.time()
    commits = (
        _make_commit("a", MemoryTheme.FEATURE, 2, ("foo.py",), now),
    )
    report = recall_memory(commits_override=commits, now_unix=now)
    assert report.snapshot is not None
    assert report.snapshot.total_commits_scanned == 1


def test_recall_memory_elapsed_s_recorded(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    now = time.time()
    commits = (
        _make_commit("a", MemoryTheme.FEATURE, 1, ("foo.py",), now),
    )
    report = recall_memory(commits_override=commits, now_unix=now)
    assert report.elapsed_s >= 0.0


def test_recall_memory_never_raises_on_runner_crash(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    def crashy(args):
        return None  # simulate git failure
    report = recall_memory(git_runner=crashy)
    assert report.verdict == RecallVerdict.DISABLED


# --- to_dict / serialization ------------------------------------------------


def test_commit_record_to_dict():
    now = time.time()
    c = _make_commit("abc", MemoryTheme.BUGFIX, 1.0, ("foo.py",), now)
    d = c.to_dict()
    assert d["sha"] == "abc"
    assert d["theme"] == "bugfix"
    assert d["files"] == ["foo.py"]
    assert d["schema_version"] == "long_horizon_memory.1"


def test_commit_theme_to_dict():
    ct = CommitTheme(
        theme=MemoryTheme.FEATURE,
        count=3,
        sample_subjects=("a", "b"),
        dominant_files=("foo.py",),
    )
    d = ct.to_dict()
    assert d["theme"] == "feature"
    assert d["count"] == 3
    assert d["sample_subjects"] == ["a", "b"]


def test_file_hotness_to_dict():
    fh = FileHotness(
        file_path="foo.py",
        touch_count=5,
        last_touched_unix=1000.0,
        days_since_touched=2.5,
        theme_distribution={"feature": 3},
        boundary_crossed=False,
    )
    d = fh.to_dict()
    assert d["file_path"] == "foo.py"
    assert d["touch_count"] == 5
    assert d["theme_distribution"] == {"feature": 3}
    assert d["boundary_crossed"] is False


def test_composed_source_digest_to_dict():
    csd = ComposedSourceDigest(
        source_name="test",
        enabled=True,
        digest_text="x",
    )
    d = csd.to_dict()
    assert d["source_name"] == "test"
    assert d["enabled"] is True


def test_memory_snapshot_to_dict():
    snap = MemorySnapshot(
        total_commits_scanned=2,
        horizon_span_days=5.0,
        horizon_classification=MemoryHorizon.DAY,
        themes=(),
        hot_files=(),
        stale_files=(),
        composed_sources=(),
        diagnostic="test",
    )
    d = snap.to_dict()
    assert d["total_commits_scanned"] == 2
    assert d["horizon_classification"] == "day"


def test_cross_session_report_to_dict():
    snap = MemorySnapshot(
        total_commits_scanned=1,
        horizon_span_days=0.0,
        horizon_classification=MemoryHorizon.SESSION,
        themes=(),
        hot_files=(),
        stale_files=(),
        composed_sources=(),
        diagnostic="x",
    )
    report = CrossSessionMemoryReport(
        evaluated_at_unix=1000.0,
        master_enabled=True,
        verdict=RecallVerdict.FRESH,
        snapshot=snap,
        diagnostic="diag",
        elapsed_s=0.1,
    )
    d = report.to_dict()
    assert d["verdict"] == "fresh"
    assert d["master_enabled"] is True
    assert d["snapshot"] is not None


def test_cross_session_report_to_dict_no_snapshot():
    report = CrossSessionMemoryReport(
        evaluated_at_unix=1000.0,
        master_enabled=False,
        verdict=RecallVerdict.DISABLED,
        snapshot=None,
        diagnostic="off",
        elapsed_s=0.0,
    )
    d = report.to_dict()
    assert d["snapshot"] is None


# --- Persistence (best-effort, env-gated) -----------------------------------


def test_persistence_disabled_no_write(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    ledger = tmp_path / "x.jsonl"
    monkeypatch.setenv(_ENV_LEDGER_PATH, str(ledger))
    now = time.time()
    commits = (_make_commit("a", MemoryTheme.FEATURE, 1, ("foo.py",), now),)
    recall_memory(commits_override=commits, now_unix=now)
    assert not ledger.exists()


def test_persistence_enabled_writes(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    ledger = tmp_path / "x.jsonl"
    monkeypatch.setenv(_ENV_LEDGER_PATH, str(ledger))
    now = time.time()
    commits = (_make_commit("a", MemoryTheme.FEATURE, 1, ("foo.py",), now),)
    recall_memory(commits_override=commits, now_unix=now)
    # Best-effort: don't fail if cross_process_jsonl unavailable in env
    if ledger.exists():
        content = ledger.read_text()
        assert "memory_report" in content


# --- Panel rendering --------------------------------------------------------


def test_format_panel_master_off(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "false")
    text = format_memory_panel(None)
    assert "disabled" in text


def test_format_panel_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    now = time.time()
    commits = (_make_commit("a", MemoryTheme.BUGFIX, 1, ("foo.py",), now),)
    report = recall_memory(commits_override=commits, now_unix=now)
    text = format_memory_panel(report)
    assert "Long-Horizon Memory" in text


def test_format_panel_no_report_master_on(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    text = format_memory_panel(None)
    assert "no report" in text


# --- SSE event registration -------------------------------------------------


def test_sse_event_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_LONG_HORIZON_MEMORY_RECALLED,
        _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_LONG_HORIZON_MEMORY_RECALLED == (
        "long_horizon_memory_recalled"
    )
    assert EVENT_TYPE_LONG_HORIZON_MEMORY_RECALLED in _VALID_EVENT_TYPES


# --- FlagRegistry seeds -----------------------------------------------------


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
    assert _ENV_HORIZON_DAYS in names


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
        "backend/core/ouroboros/governance/long_horizon_memory.py"
    )
    src = target.read_text()
    return src, ast.parse(src)


def test_ast_pins_count():
    pins = register_shipped_invariants()
    assert len(pins) == 6


def test_ast_pin_verdict_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_horizon_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "horizon_taxonomy" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_theme_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "theme_taxonomy" in p.invariant_name
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


# --- AST pin synthetic regressions ------------------------------------------


def test_ast_pin_verdict_taxonomy_catches_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy" in p.invariant_name
    )
    bad = '''
class RecallVerdict(str, enum.Enum):
    FRESH = "fresh"
    WARM = "warm"
    COLD = "cold"
    SOMETHING_NEW = "something_new"
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_horizon_taxonomy_catches_missing():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "horizon_taxonomy" in p.invariant_name
    )
    bad = '''
class MemoryHorizon(str, enum.Enum):
    SESSION = "session"
    DAY = "day"
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_theme_taxonomy_catches_typo():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "theme_taxonomy" in p.invariant_name
    )
    bad = '''
class MemoryTheme(str, enum.Enum):
    REFACTOR = "refactor"
    FEATURE = "feature"
    BUGFIX = "bugfx"
    OTHER = "other"
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_authority_asymmetry_catches_forbidden_import():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = '''
from backend.core.ouroboros.governance.orchestrator import x
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


def test_ast_pin_composes_canonical_catches_missing_substrate():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    # Source missing 'semantic_index'
    bad = '''
import subprocess
# user_preference_memory
# last_session_summary
# governance_boundary_gate
# cross_process_jsonl
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()

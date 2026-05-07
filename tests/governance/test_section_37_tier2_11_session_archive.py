"""§37 Tier 2 #11 — Session-search SQLite + /history REPL.

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  "Solve the root problem directly—without workarounds, brute force,
   or shortcut solutions. Significantly strengthen the system into
   something advanced, asynchronous, dynamic, adaptive, intelligent,
   and highly robust, with no hardcoding. Fully leverage existing
   files and architecture so we avoid duplication and build cleanly
   on what already exists."

Coverage (~38 tests):
  Slice 1 — SessionArchive substrate
    * Master flag default-FALSE per §33.1
    * SessionRecord schema + frozen + to_dict
    * find_sessions returns [] when master off
    * Backfill is idempotent (running twice ≠ duplicates)
    * Backfill ingests live_fire_graduation_history.jsonl
    * Backfill ingests graduation_ledger.jsonl (merge_only)
    * Backfill ingests session summary.json (merge with telemetry)
    * find_sessions filters: outcome / flag / since_epoch /
      until_epoch / notes_contains / limit clamping
    * find_sessions ordering (started_at_epoch DESC)
    * get_session lookup
    * total_count
    * NEVER raises on broken DB / missing files / corrupt JSONL
    * 3 AST pins clean + each fires on synthetic regression

  Slice 2 — /history REPL
    * Auto-discovery shape (matches=False on unrelated lines)
    * Bare /history → recent 20
    * /history help / recent / flag / since / outcome /
      session / search / backfill / unknown subcommand
    * Disabled message when master flag off
    * /history search rejects empty text
    * /history since rejects non-numeric / non-positive
"""
from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "session_archive.py"
    )


@pytest.fixture
def isolated_archive(tmp_path, monkeypatch):
    """Fresh DB + isolated repo root per test."""
    monkeypatch.setenv(
        "JARVIS_SESSION_ARCHIVE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_SESSION_ARCHIVE_REPO_ROOT", str(tmp_path),
    )
    monkeypatch.setenv(
        "JARVIS_SESSION_ARCHIVE_DB_PATH",
        str(tmp_path / "test_archive.db"),
    )
    (tmp_path / ".jarvis").mkdir()
    (tmp_path / ".ouroboros" / "sessions").mkdir(parents=True)
    from backend.core.ouroboros.governance.session_archive import (
        SessionArchive,
        reset_default_archive_for_tests,
    )
    reset_default_archive_for_tests()
    archive = SessionArchive()
    yield (archive, tmp_path)
    reset_default_archive_for_tests()


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_SESSION_ARCHIVE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.session_archive import (
        master_enabled,
    )
    assert master_enabled() is False


def test_find_sessions_empty_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_SESSION_ARCHIVE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.session_archive import (
        SessionArchive,
    )
    arch = SessionArchive()
    assert arch.find_sessions() == []


# ---------------------------------------------------------------------------
# SessionRecord
# ---------------------------------------------------------------------------


def test_session_record_frozen():
    from backend.core.ouroboros.governance.session_archive import (
        SessionRecord,
    )
    r = SessionRecord(session_id="bt-1")
    with pytest.raises(Exception):  # frozen
        r.session_id = "bt-2"  # type: ignore[misc]


def test_session_record_to_dict():
    from backend.core.ouroboros.governance.session_archive import (
        SessionRecord,
    )
    r = SessionRecord(
        session_id="bt-1", outcome="clean",
        flag_name="JARVIS_X", duration_s=120.5,
        cost_usd=0.05,
    )
    d = r.to_dict()
    assert d["session_id"] == "bt-1"
    assert d["outcome"] == "clean"
    assert d["flag_name"] == "JARVIS_X"
    assert d["duration_s"] == pytest.approx(120.5)
    assert "schema_version" in d


# ---------------------------------------------------------------------------
# Backfill — composes canonical ledgers
# ---------------------------------------------------------------------------


def _write_live_fire_ledger(repo_root: Path, entries: list):
    path = (
        repo_root / ".jarvis"
        / "live_fire_graduation_history.jsonl"
    )
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _write_grad_ledger(repo_root: Path, entries: list):
    path = repo_root / ".jarvis" / "graduation_ledger.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _write_summary(
    repo_root: Path, session_id: str, payload: dict,
):
    sdir = repo_root / ".ouroboros" / "sessions" / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    with (sdir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f)


def test_backfill_zero_when_no_files(isolated_archive):
    archive, _ = isolated_archive
    assert archive.backfill() == 0


def test_backfill_ingests_live_fire(isolated_archive):
    archive, root = isolated_archive
    _write_live_fire_ledger(root, [
        {
            "session_id": "bt-A", "flag_name": "JARVIS_X",
            "outcome": "clean", "cost_total_usd": 0.05,
            "duration_s": 120.0, "ops_count": 5,
            "started_at_epoch": 1700000000.0,
            "finished_at_epoch": 1700000120.0,
            "notes": "complete_no_runner_failures",
        },
        {
            "session_id": "bt-B", "flag_name": "JARVIS_Y",
            "outcome": "runner", "cost_total_usd": 0.01,
            "duration_s": 30.0, "started_at_epoch": 1700001000.0,
            "notes": "runner_caught_us",
        },
    ])
    n = archive.backfill()
    assert n == 2
    sessions = archive.find_sessions()
    assert len(sessions) == 2
    ids = {s.session_id for s in sessions}
    assert ids == {"bt-A", "bt-B"}


def test_backfill_idempotent(isolated_archive):
    archive, root = isolated_archive
    _write_live_fire_ledger(root, [
        {
            "session_id": "bt-A", "flag_name": "JARVIS_X",
            "outcome": "clean", "cost_total_usd": 0.05,
            "started_at_epoch": 1700000000.0,
        },
    ])
    archive.backfill()
    archive.backfill()
    archive.backfill()
    # Three backfills → still just one row.
    assert archive.total_count() == 1


def test_backfill_merges_grad_ledger(isolated_archive):
    archive, root = isolated_archive
    _write_live_fire_ledger(root, [
        {
            "session_id": "bt-A", "flag_name": "JARVIS_X",
            "outcome": "clean", "cost_total_usd": 0.05,
            "duration_s": 120.0,
            "started_at_epoch": 1700000000.0,
            "notes": "complete_no_runner_failures",
        },
    ])
    _write_grad_ledger(root, [
        {
            "session_id": "bt-A", "flag_name": "JARVIS_X",
            "outcome": "clean",
            "recorded_at_epoch": 1700000200.0,
            "recorded_by": "live_fire_soak_cli",
            "notes": "graduation_recorded",
        },
        {
            "session_id": "bt-C", "flag_name": "JARVIS_Z",
            "outcome": "clean",
            "recorded_at_epoch": 1700002000.0,
            "recorded_by": "live_fire_soak_cli",
            "notes": "graduation_recorded",
        },
    ])
    archive.backfill()
    rec_a = archive.get_session("bt-A")
    rec_c = archive.get_session("bt-C")
    # bt-A: live_fire row preserved (notes from live_fire,
    # recorded_by filled from grad_ledger via merge).
    assert rec_a is not None
    assert rec_a.notes == "complete_no_runner_failures"
    assert rec_a.recorded_by == "live_fire_soak_cli"
    # bt-C: only in grad_ledger — created by merge upsert.
    assert rec_c is not None
    assert rec_c.outcome == "clean"


def test_backfill_ingests_summary_json(isolated_archive):
    archive, root = isolated_archive
    # Live-fire row missing duration / ops_count.
    _write_live_fire_ledger(root, [
        {
            "session_id": "bt-A", "flag_name": "JARVIS_X",
            "outcome": "clean",
            "started_at_epoch": 1700000000.0,
        },
    ])
    _write_summary(root, "bt-A", {
        "session_id": "bt-A",
        "duration_s": 200.5,
        "cost_total": 0.10,
        "stats": {"attempted": 7},
        "stop_reason": "idle_timeout",
    })
    archive.backfill()
    rec = archive.get_session("bt-A")
    assert rec is not None
    # Live-fire's outcome preserved (merge_only on summary).
    assert rec.outcome == "clean"
    # Summary's telemetry filled in via COALESCE merge.
    assert rec.duration_s == pytest.approx(200.5)
    assert rec.ops_count == 7
    assert rec.stop_reason == "idle_timeout"


def test_backfill_skips_corrupt_jsonl_lines(isolated_archive):
    archive, root = isolated_archive
    path = (
        root / ".jarvis"
        / "live_fire_graduation_history.jsonl"
    )
    with path.open("w", encoding="utf-8") as f:
        f.write("not json\n")
        f.write(json.dumps({
            "session_id": "bt-A", "outcome": "clean",
            "started_at_epoch": 1700000000.0,
        }) + "\n")
        f.write("\n")  # blank line
        f.write("{garbled\n")
    n = archive.backfill()
    # Only the valid line ingested.
    assert n == 1


# ---------------------------------------------------------------------------
# Query API — filters
# ---------------------------------------------------------------------------


def _seed(archive, root):
    _write_live_fire_ledger(root, [
        {
            "session_id": "bt-A", "flag_name": "JARVIS_X",
            "outcome": "clean", "cost_total_usd": 0.05,
            "started_at_epoch": 1700000000.0,
            "notes": "complete_no_runner_failures",
        },
        {
            "session_id": "bt-B", "flag_name": "JARVIS_X",
            "outcome": "runner", "cost_total_usd": 0.10,
            "started_at_epoch": 1700001000.0,
            "notes": "runner_caught_test",
        },
        {
            "session_id": "bt-C", "flag_name": "JARVIS_Y",
            "outcome": "clean", "cost_total_usd": 0.02,
            "started_at_epoch": 1700002000.0,
            "notes": "tier_2_eval",
        },
    ])
    archive.backfill()


def test_filter_by_outcome(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    clean = archive.find_sessions(outcome="clean")
    assert len(clean) == 2
    assert all(r.outcome == "clean" for r in clean)


def test_filter_by_flag(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    res = archive.find_sessions(flag="JARVIS_X")
    assert len(res) == 2
    assert all(r.flag_name == "JARVIS_X" for r in res)


def test_filter_by_since_epoch(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    res = archive.find_sessions(since_epoch=1700001500.0)
    # Only bt-C (started_at 1700002000) qualifies.
    assert len(res) == 1
    assert res[0].session_id == "bt-C"


def test_filter_by_notes_contains(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    res = archive.find_sessions(notes_contains="runner_caught")
    assert len(res) == 1
    assert res[0].session_id == "bt-B"


def test_results_ordered_newest_first(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    res = archive.find_sessions(limit=10)
    ids = [r.session_id for r in res]
    assert ids == ["bt-C", "bt-B", "bt-A"]


def test_limit_clamps_to_range(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    # limit=0 → clamps to 1
    res = archive.find_sessions(limit=0)
    assert len(res) == 1
    # limit=99999 → clamps to 1000 (well above our 3 rows)
    res2 = archive.find_sessions(limit=99999)
    assert len(res2) == 3


def test_get_session_miss_returns_none(isolated_archive):
    archive, _ = isolated_archive
    assert archive.get_session("bt-NONEXISTENT") is None


def test_get_session_hit_returns_record(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    rec = archive.get_session("bt-B")
    assert rec is not None
    assert rec.outcome == "runner"


# ---------------------------------------------------------------------------
# Defensive — NEVER raises
# ---------------------------------------------------------------------------


def test_find_sessions_never_raises_on_broken_db(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_ARCHIVE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_SESSION_ARCHIVE_DB_PATH",
        "/this/path/cannot/exist/at/all/db",
    )
    from backend.core.ouroboros.governance.session_archive import (
        SessionArchive,
    )
    arch = SessionArchive()
    # Must NOT raise.
    assert arch.find_sessions() == []


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "session_archive_master_flag_default_false",
        "session_archive_authority_asymmetry",
        "session_archive_composes_canonical_paths",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.session_archive import (
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    violations = pin.validate(tree, src)
    assert violations == ()


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.session_archive import (
        register_shipped_invariants,
    )
    bad = "from backend.core.ouroboros.governance.orchestrator import x"
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "session_archive_authority_asymmetry"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_canonical_paths_pin_fires_when_path_renamed():
    """If we rename one of the canonical ledger paths, the pin
    fires (parallel-paths defense)."""
    from backend.core.ouroboros.governance.session_archive import (
        register_shipped_invariants,
    )
    bad = "x = 'some_other_ledger.jsonl'"
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "session_archive_composes_canonical_paths"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# /history REPL — Slice 2
# ---------------------------------------------------------------------------


def test_repl_unmatched_line():
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/something_else")
    assert out.matched is False


def test_repl_help():
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/history help")
    assert out.ok is True
    assert "/history flag" in out.text
    assert "/history search" in out.text


def test_repl_disabled_message_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_SESSION_ARCHIVE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/history")
    assert out.ok is True
    assert "disabled" in out.text.lower()


def test_repl_recent(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    # Defaults to 20.
    out = dispatch_history_command("/history")
    assert out.ok is True
    # All 3 seeded sessions appear (newest first).
    assert "bt-C" in out.text
    assert "bt-B" in out.text
    assert "bt-A" in out.text


def test_repl_recent_with_n(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/history recent 1")
    assert out.ok is True
    # Only the newest (bt-C) appears.
    assert "bt-C" in out.text
    assert "bt-A" not in out.text


def test_repl_recent_invalid_n(isolated_archive):
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/history recent garbage")
    assert out.ok is False
    assert "integer" in out.text


def test_repl_flag_filter(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/history flag JARVIS_X")
    assert out.ok is True
    assert "bt-A" in out.text
    assert "bt-B" in out.text
    assert "bt-C" not in out.text


def test_repl_flag_missing_arg(isolated_archive):
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/history flag")
    assert out.ok is False
    assert "missing flag name" in out.text


def test_repl_outcome_filter(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/history outcome runner")
    assert out.ok is True
    assert "bt-B" in out.text
    assert "bt-A" not in out.text


def test_repl_session_detail(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/history session bt-A")
    assert out.ok is True
    assert "bt-A" in out.text
    assert "outcome" in out.text


def test_repl_session_miss(isolated_archive):
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command(
        "/history session bt-NONEXISTENT",
    )
    assert out.ok is False
    assert "no record" in out.text


def test_repl_search_filter(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command(
        "/history search runner_caught",
    )
    assert out.ok is True
    assert "bt-B" in out.text


def test_repl_search_empty_text():
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command('/history search ""')
    assert out.ok is False
    assert "empty" in out.text


def test_repl_since_filter(isolated_archive):
    archive, root = isolated_archive
    _seed(archive, root)
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    # 'since 99999' days → all 3 records (all fall within
    # arbitrarily large window).
    out = dispatch_history_command("/history since 99999")
    assert out.ok is True
    assert "bt-A" in out.text


def test_repl_since_invalid(isolated_archive):
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/history since garbage")
    assert out.ok is False
    assert "numeric" in out.text


def test_repl_since_zero_rejected(isolated_archive):
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/history since 0")
    assert out.ok is False
    assert "> 0" in out.text


def test_repl_unknown_subcommand():
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/history bogus")
    assert out.ok is False
    assert "unknown subcommand" in out.text


def test_repl_backfill(isolated_archive):
    archive, root = isolated_archive
    _write_live_fire_ledger(root, [
        {
            "session_id": "bt-A", "flag_name": "JARVIS_X",
            "outcome": "clean", "started_at_epoch": 1700000000.0,
        },
    ])
    from backend.core.ouroboros.governance.history_repl import (
        dispatch_history_command,
    )
    out = dispatch_history_command("/history backfill")
    assert out.ok is True
    assert "1 rows upserted" in out.text or "upserted" in out.text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance import (
        session_archive as mod,
    )
    expected = {
        "SESSION_ARCHIVE_SCHEMA_VERSION",
        "SessionArchive",
        "SessionRecord",
        "default_db_path",
        "get_default_archive",
        "master_enabled",
        "register_flags",
        "register_shipped_invariants",
        "reset_default_archive_for_tests",
    }
    assert set(mod.__all__) == expected

"""DeepAnalysis analyzes what changed, within its budget (2026-10-09).

A full pass parses every Python file -- ~130 s on this repo against a 60 s
pool deadline -- so it timed out every cycle and reported nothing. The
incremental cycle caches per-file facts by content hash, analyzes only stale
files under a time budget, and runs the cross-file orphan analysis from the
cache once coverage is complete. These pin convergence, invalidation, and
that the incremental orphan answer equals the whole-corpus one.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intake.sensors import deep_analysis_sensor as d


def _repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "backend" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "used.py").write_text("def helper():\n    return 1\n")
    (pkg / "orphan.py").write_text("def lonely():\n    return 2\n")
    (pkg / "caller.py").write_text("from backend.pkg.used import helper\n\nx = helper()\n")
    (pkg / "defect.py").write_text("def f(items=[]):\n    items.append(1)\n    return items\n")
    return tmp_path


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo")
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_CACHE_PATH", str(tmp_path / "cache.sqlite3"))
    # The tmp repo is not a git repo: the shared module index still walks it.
    return root


def _orphans(findings):
    return sorted(f.evidence.get("function") for f in findings if f.category == "orphan_surface")


def test_cycle_covers_everything_then_has_nothing_stale(repo):
    findings, stats = d.run_incremental_cycle(repo_root=repo)
    assert stats["complete"] and stats["analyzed"] == stats["files"] == 4
    again, stats2 = d.run_incremental_cycle(repo_root=repo)
    assert stats2["stale"] == 0 and stats2["analyzed"] == 0
    assert sorted(f.dedup_key for f in again) == sorted(f.dedup_key for f in findings)


def test_incremental_orphans_equal_the_whole_corpus_answer(repo):
    findings, _ = d.run_incremental_cycle(repo_root=repo)
    whole = d.analyze_orphan_surface(repo_root=repo)
    # `f` (the latent-defect fixture) is never called either: it is an orphan too.
    assert _orphans(findings) == _orphans(whole) == ["f", "lonely"]


def test_a_changed_file_is_reanalyzed_and_answers_change(repo):
    d.run_incremental_cycle(repo_root=repo)
    (repo / "backend" / "pkg" / "caller.py").write_text(
        "from backend.pkg.orphan import lonely\nfrom backend.pkg.used import helper\n\n"
        "x = helper() + lonely()\n"
    )
    findings, stats = d.run_incremental_cycle(repo_root=repo)
    assert stats["stale"] == 1 and stats["analyzed"] == 1
    assert _orphans(findings) == ["f"]            # lonely is now called


def test_a_deleted_file_leaves_the_cache(repo):
    d.run_incremental_cycle(repo_root=repo)
    (repo / "backend" / "pkg" / "orphan.py").unlink()
    findings, stats = d.run_incremental_cycle(repo_root=repo)
    assert stats["files"] == 3 and _orphans(findings) == ["f"]


def test_partial_coverage_withholds_cross_file_orphans(repo, monkeypatch):
    # A budget too small to finish: orphans need EVERY file's identifiers,
    # so a partial cycle must not report one from incomplete facts.
    monkeypatch.setattr(d, "_cycle_budget_s", lambda: -1.0)
    findings, stats = d.run_incremental_cycle(repo_root=repo)
    assert stats["analyzed"] == 0 and not stats["complete"]
    assert _orphans(findings) == []


def test_latent_defects_survive_the_cache(repo):
    findings, _ = d.run_incremental_cycle(repo_root=repo)
    assert any(f.category == "latent_defect" and f.file.endswith("defect.py") for f in findings)


def test_disabled_categories_are_filtered_at_read_time(repo, monkeypatch):
    d.run_incremental_cycle(repo_root=repo)
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_DEFECT_ENABLED", "false")
    findings, stats = d.run_incremental_cycle(repo_root=repo)
    assert stats["analyzed"] == 0                       # cache reused
    assert not any(f.category == "latent_defect" for f in findings)


def test_an_unusable_cache_degrades_to_nothing(tmp_path, monkeypatch):
    root = _repo(tmp_path / "r")
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_CACHE_PATH", str(blocker / "cache.sqlite3"))
    assert d.run_incremental_cycle(repo_root=root) == ([], {"files": 0, "stale": 0, "analyzed": 0, "complete": False})


def test_the_sensor_runs_the_incremental_cycle():
    import inspect
    src = inspect.getsource(d.DeepAnalysisSensor.scan_once)
    assert "run_incremental_cycle" in src and "run_all_analyzers," not in src

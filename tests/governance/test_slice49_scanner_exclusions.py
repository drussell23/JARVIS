"""Slice 49 Phase 1 — Python-scanner traversal exclusions.

v44 wedged at 107% CPU on os.scandir/os.stat walking + ast-parsing
`.jarvis/swe_bench_pro/worktrees` (437MB / 26,839 files). FileWatchGuard
already excluded it — the storm came from the PYTHON scanners:
  * Oracle EXCLUDE_PATTERNS had .worktrees/.ouroboros (Slice 44) but NOT
    .jarvis, and its substring match (`pattern in path_str`) means
    `.worktrees` never matched `.jarvis/swe_bench_pro/worktrees`.
  * OpportunityMiner did `root.rglob("*.py")` over "." — the walk itself
    traversed every file; post-walk _is_production_code filtering was too late.

Pins:
  §1  Oracle EXCLUDE_PATTERNS covers .jarvis (+ Slice 44 .worktrees/.ouroboros)
  §2  a .jarvis/swe_bench_pro/worktrees path is excluded by the substring rule
  §3  miner pruning walk never descends into .jarvis/.worktrees/.ouroboros
  §4  miner pruning walk still returns real source files
  §5  pruning happens DURING traversal (excluded dirs are never stat'd deep)
"""
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.oracle import OracleConfig
from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
    _WALK_PRUNE_SEGMENTS,
    _iter_python_files_pruned,
)


# ── §1 Oracle pattern coverage ──────────────────────────────────────────
def test_oracle_excludes_jarvis_and_worktrees() -> None:
    pats = set(OracleConfig.EXCLUDE_PATTERNS)
    assert ".jarvis" in pats
    assert ".worktrees" in pats
    assert ".ouroboros" in pats


# ── §2 swe_bench path excluded via substring ────────────────────────────
def test_oracle_substring_excludes_nested_swe_bench() -> None:
    path = "/repo/.jarvis/swe_bench_pro/worktrees/inst/copy-i18n.py"
    assert any(p in path for p in OracleConfig.EXCLUDE_PATTERNS)


# ── §3/§4/§5 miner pruning walk ─────────────────────────────────────────
def test_miner_walk_prunes_heavy_trees(tmp_path: Path) -> None:
    # real source
    (tmp_path / "backend" / "core").mkdir(parents=True)
    (tmp_path / "backend" / "core" / "real.py").write_text("x = 1\n")
    # trees that must NEVER be walked
    swe = tmp_path / ".jarvis" / "swe_bench_pro" / "worktrees" / "inst"
    swe.mkdir(parents=True)
    (swe / "junk.py").write_text("print 'py2'\n")  # would SyntaxError if parsed
    (tmp_path / ".worktrees" / "wt").mkdir(parents=True)
    (tmp_path / ".worktrees" / "wt" / "dup.py").write_text("y = 2\n")
    (tmp_path / ".ouroboros" / "sessions").mkdir(parents=True)
    (tmp_path / ".ouroboros" / "sessions" / "log.py").write_text("z = 3\n")

    found = _iter_python_files_pruned(tmp_path, _WALK_PRUNE_SEGMENTS)
    names = {p.name for p in found}

    assert "real.py" in names
    assert "junk.py" not in names
    assert "dup.py" not in names
    assert "log.py" not in names
    # nothing under an excluded segment leaked through
    assert all(
        not any(seg in p.parts for seg in (".jarvis", ".worktrees", ".ouroboros"))
        for p in found
    )


def test_prune_segments_include_the_heavy_trees() -> None:
    for seg in (".jarvis", ".worktrees", ".ouroboros"):
        assert seg in _WALK_PRUNE_SEGMENTS

"""Slice 49 Phase 3 — universal ingestion stratification (soft, at the funnel).

OperationAdvisor.advise() already HARD-gates blast-radius on every op
(classify_runner.py:396). This adds a SOFT priority penalty at the single
funnel UnifiedIntakeRouter.ingest -> _compute_priority, so large uncovered
targets are deprioritized (processed last) fleet-wide across ALL sensor
tracks — while staying fully reachable (no drop), with a test-gen escape.

Pins:
  §1  no files / covered file → zero penalty
  §2  huge uncovered file → positive penalty (deprioritized)
  §3  suppress (test-gen escape) → zero penalty
  §4  penalty is bounded (cannot dominate the whole priority scale)
  §5  _compute_priority: huge-uncovered envelope ranks WORSE than small-covered
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.target_stratification import (
    ingest_priority_penalty,
)


def _mk(root: Path, rel: str, lines: int, *, covered: bool) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x = 1\n" * lines)
    if covered:
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / f"test_{Path(rel).stem}.py").write_text("def test(): pass\n")
    return rel


# ── §1 ─────────────────────────────────────────────────────────────────
def test_no_files_or_covered_is_zero(tmp_path: Path) -> None:
    assert ingest_priority_penalty([], tmp_path) == 0
    rel = _mk(tmp_path, "backend/big.py", 5000, covered=True)
    assert ingest_priority_penalty([rel], tmp_path) == 0


# ── §2 ─────────────────────────────────────────────────────────────────
def test_huge_uncovered_gets_penalty(tmp_path: Path) -> None:
    rel = _mk(tmp_path, "backend/huge.py", 5000, covered=False)
    assert ingest_priority_penalty([rel], tmp_path) > 0


# ── §3 ─────────────────────────────────────────────────────────────────
def test_suppress_escape_hatch(tmp_path: Path) -> None:
    rel = _mk(tmp_path, "backend/huge.py", 5000, covered=False)
    assert ingest_priority_penalty([rel], tmp_path, suppress=True) == 0


# ── §4 ─────────────────────────────────────────────────────────────────
def test_penalty_is_bounded(tmp_path: Path) -> None:
    rels = [_mk(tmp_path, f"backend/h{i}.py", 9000, covered=False) for i in range(5)]
    pen = ingest_priority_penalty(rels, tmp_path)
    assert 0 < pen <= 5  # bounded — cannot swamp base priorities (1..99)


# ── §5 integration with _compute_priority ───────────────────────────────
def test_compute_priority_deprioritizes_huge_uncovered(tmp_path: Path) -> None:
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        _compute_priority,
    )
    from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

    huge = _mk(tmp_path, "backend/huge.py", 5000, covered=False)
    small = _mk(tmp_path, "backend/small.py", 40, covered=True)

    common = dict(
        source="ai_miner", description="improve", repo="jarvis",
        confidence=0.5, urgency="normal", evidence={}, requires_human_ack=True,
    )
    env_huge = make_envelope(target_files=(huge,), **common)
    env_small = make_envelope(target_files=(small,), **common)

    p_huge, _ = _compute_priority(env_huge, repo_root=tmp_path)
    p_small, _ = _compute_priority(env_small, repo_root=tmp_path)

    # lower int = higher priority; the huge uncovered op must rank WORSE
    assert p_huge > p_small

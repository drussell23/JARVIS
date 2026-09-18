"""Work that can finish outranks work that was merely filed first.

Five soaks produced nothing while a landable goal sat at index 26 of 27. The
cause was not the model and not the pipeline: every roadmap goal carries the
same 0.75 evidence weight, so the ranking sort had equal keys everywhere and
degraded to DOCUMENT ORDER on an append-only file. With the Sentinel taking
``candidates[0]`` and a cap of 8, the queue was a window onto the oldest eight
goals — and the only one that could land was not among them.

The fix ranks, it does not shed: a goal whose target file does not exist is
usually a test-synthesis goal that CREATES that file, and is the only thing
that can unblock the repair goals waiting behind it.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy import goal_discovery as gd


def _work(target: str, *, kind: str = "roadmap", weight: float = 0.75):
    return gd.DiscoveredWork(target_file=target, kind=kind, evidence="e", weight=weight)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "backend").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "backend" / "covered.py").write_text("x = 1\n")
    (tmp_path / "backend" / "bare.py").write_text("x = 1\n")
    (tmp_path / "tests" / "test_covered.py").write_text("def test_x(): pass\n")
    return tmp_path


# --------------------------------------------------------------------------
# Phase 1 — the tiers
# --------------------------------------------------------------------------

def test_the_four_tiers(repo: Path):
    cov = gd._covering_test_stems(repo)
    assert gd._liveness_rank(_work("backend/covered.py"), repo, cov) == gd.LIVENESS_LANDABLE
    assert gd._liveness_rank(_work("tests/test_new.py"), repo, cov) == gd.LIVENESS_CREATES_TEST
    assert gd._liveness_rank(_work("backend/bare.py"), repo, cov) == gd.LIVENESS_NO_TEST
    assert gd._liveness_rank(_work("backend/gone.py"), repo, cov) == gd.LIVENESS_DEAD


def test_a_goal_that_creates_its_target_test_is_LIVE_not_dead(repo: Path):
    """The whole reason this ranks rather than sheds. Dropping these would
    delete the DAG's test-synthesis half — the half that makes the repair
    goals behind it landable."""
    w = _work("tests/test_does_not_exist_yet.py")
    assert gd._liveness_rank(w, repo) > gd.LIVENESS_DEAD
    assert gd.is_dispatchable(w, repo) is True


def test_an_existing_test_file_is_its_own_cover(repo: Path):
    assert gd._liveness_rank(_work("tests/test_covered.py"), repo) == gd.LIVENESS_LANDABLE


def test_an_ambient_red_is_never_demoted_by_a_NAMING_convention(repo: Path):
    """A failing test proves both that work is needed and that something
    exercises the file. Running it through `test_<stem>.py` would sink hard
    evidence below speculative test-writing over a filename."""
    red = _work("backend/bare.py", kind="ambient_red", weight=1.0)
    assert gd._liveness_rank(red, repo, frozenset()) == gd.LIVENESS_LANDABLE


def test_liveness_outranks_evidence_weight(repo: Path):
    """Deliberate: strong evidence about a file that is not there cannot land,
    and an op spent on it is an op not spent on work that can."""
    cov = gd._covering_test_stems(repo)
    q = [_work("backend/gone.py", weight=1.0), _work("backend/covered.py", weight=0.4)]
    ordered = sorted(q, key=lambda w: (-gd._liveness_rank(w, repo, cov), -w.weight))
    assert ordered[0].target_file == "backend/covered.py"


def test_weight_still_breaks_ties_INSIDE_a_tier(repo: Path):
    """Liveness reorders across tiers only. Within one, the existing evidence
    ranking is untouched."""
    cov = gd._covering_test_stems(repo)
    (repo / "backend" / "other.py").write_text("x = 1\n")
    (repo / "tests" / "test_other.py").write_text("def test_y(): pass\n")
    cov = gd._covering_test_stems(repo)
    q = [_work("backend/covered.py", weight=0.4), _work("backend/other.py", weight=0.9)]
    ordered = sorted(q, key=lambda w: (-gd._liveness_rank(w, repo, cov), -w.weight))
    assert [w.weight for w in ordered] == [0.9, 0.4]


def test_the_starvation_itself(repo: Path):
    """THE regression. Equal weights + append-only document = oldest-first,
    and the landable goal is last."""
    cov = gd._covering_test_stems(repo)
    document_order = [
        _work("backend/gone_a.py"), _work("backend/gone_b.py"),
        _work("backend/bare.py"), _work("backend/covered.py"),
    ]
    assert document_order[-1].target_file == "backend/covered.py"
    ranked = sorted(
        document_order, key=lambda w: (-gd._liveness_rank(w, repo, cov), -w.weight),
    )
    assert ranked[0].target_file == "backend/covered.py"
    assert ranked[-1].target_file.startswith("backend/gone")


def test_the_covering_index_is_built_ONCE_not_per_candidate(repo: Path, monkeypatch):
    """`sorted` calls its key per element; an rglob in there is N walks of the
    test tree on the Sentinel's critical path."""
    calls = {"n": 0}
    real = gd._covering_test_stems

    def _counted(root):
        calls["n"] += 1
        return real(root)

    monkeypatch.setattr(gd, "_covering_test_stems", _counted)
    asyncio.run(gd.discover(repo_root=repo, limit=4))
    # Exactly one, not "at most one": zero would mean the sort never consulted
    # the index at all, and this test would pass while proving nothing.
    assert calls["n"] == 1


def test_ranking_never_raises(repo: Path):
    for bad in ("", "\\\\?\\nope", "backend/../../etc/passwd"):
        assert gd._liveness_rank(_work(bad), repo) >= gd.LIVENESS_DEAD
    assert gd._liveness_rank(_work("x"), Path("/does/not/exist")) >= gd.LIVENESS_DEAD


def test_a_windows_separator_still_reads_as_a_test(repo: Path):
    assert gd._liveness_rank(_work("tests\\test_new.py"), repo) == gd.LIVENESS_CREATES_TEST


# --------------------------------------------------------------------------
# Phase 3 — the dispatcher refuses dead work
# --------------------------------------------------------------------------

def test_is_dispatchable_sheds_ONLY_the_dead_tier(repo: Path):
    assert gd.is_dispatchable(_work("backend/gone.py"), repo) is False
    for live in ("backend/covered.py", "backend/bare.py", "tests/test_new.py"):
        assert gd.is_dispatchable(_work(live), repo) is True


def test_unlikely_to_land_is_not_the_same_as_nothing_to_edit(repo: Path):
    """L1 sheds at VALIDATE for want of a test. It is still real work on a
    real file, and deciding that is the pipeline's job, not the queue's."""
    assert gd.is_dispatchable(_work("backend/bare.py"), repo) is True


def test_a_windows_separator_still_finds_an_EXISTING_file(repo: Path):
    """The roadmap is authored from both trees. A backslash path is one path
    component on POSIX, so an un-normalised existence check would call a
    landable goal a test-synthesis goal."""
    cov = gd._covering_test_stems(repo)
    w = _work(r"backend\covered.py")
    assert gd._liveness_rank(w, repo, cov) == gd.LIVENESS_LANDABLE

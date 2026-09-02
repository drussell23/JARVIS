"""Hunk-level structural similarity: compare what a sibling CHANGED.

Soak `bt-2026-09-01-235803` exposed the dilution: candidates are whole-file
rewrites of 80-150 line modules, so two genuinely different implementations
of one small edit still shared 96-99% of the tree and were rejected as
redundant at any sane threshold -- the unchanged file dominated the ratio.
Measured on the exact shape reproduced below: whole-file similarity 0.9564
(redundant at 0.95), hunk-level 0.4866 (distinct).

The change reuses the value gate's own primitives (`_stripped_tree`, the
per-statement `ast.dump` opcode walk `_python_semantic_weight` already runs
against the on-disk file), so "what changed" has one definition. Without a
baseline -- a created file, an unparseable one, or a caller that has no
repo root -- behaviour is byte-identical to the whole-file fingerprint.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import sibling_entropy as se

_BASE = "".join(
    f"def handler_{i}(payload):\n"
    f"    total = 0\n"
    f"    for item in payload:\n"
    f"        if item.get('active'):\n"
    f"            total += item['size']\n"
    f"    return {{'n': total}}\n\n"
    for i in range(8)
)
# Two DIFFERENT implementations of the same small addition.
_BRANCH = _BASE + (
    "def fallback(kind):\n"
    "    if kind == 'network':\n"
    "        return {'retry': True, 'delay': 1000}\n"
    "    return {'retry': False}\n"
)
_TABLE = _BASE + (
    "_TABLE = {'network': {'retry': True, 'delay': 1000}}\n\n"
    "def fallback(kind):\n"
    "    return _TABLE.get(kind, {'retry': False})\n"
)
_BRANCH_TWIN = _BRANCH.replace(
    "return {'retry': False}", "return {'retry': False}  # unchanged logic",
)


# --------------------------------------------------------------------------
# The dilution, and its removal
# --------------------------------------------------------------------------


def test_whole_file_similarity_is_diluted_by_the_unchanged_file() -> None:
    """The soak-10 measurement, reproduced: 0.956 whole-file for two
    different algorithms, because 95% of the tree is the same file."""
    sim = se.structural_similarity(
        se.structural_fingerprint(_BRANCH), se.structural_fingerprint(_TABLE),
    )
    assert sim > 0.95
    redundant, _ = se.is_structurally_redundant(
        [se.structural_fingerprint(_TABLE)], [se.structural_fingerprint(_BRANCH)],
        threshold=0.95,
    )
    assert redundant is True, "whole-file mode calls two algorithms one answer"


def test_hunk_level_sees_two_different_answers() -> None:
    a = se.structural_fingerprint(_BRANCH, _BASE)
    b = se.structural_fingerprint(_TABLE, _BASE)
    sim = se.structural_similarity(a, b)
    assert sim < 0.6
    redundant, _ = se.is_structurally_redundant([b], [a], threshold=0.95)
    assert redundant is False


def test_a_comment_only_twin_is_still_one_answer_at_hunk_level() -> None:
    """Removing the dilution must not start counting presentation."""
    a = se.structural_fingerprint(_BRANCH, _BASE)
    t = se.structural_fingerprint(_BRANCH_TWIN, _BASE)
    assert a == t
    assert se.structural_similarity(a, t) == 1.0


def test_change_nothing_is_a_real_answer_not_an_absence() -> None:
    """A candidate identical to the baseline fingerprints as '' -- a value.

    Two of those are rightly the same answer; None would have meant
    'unparseable' and silently excluded them from the comparison."""
    fp = se.structural_fingerprint(_BASE, _BASE)
    assert fp == ""
    redundant, peak = se.is_structurally_redundant([fp], [fp])
    assert redundant is True and peak == 1.0


def test_no_baseline_is_byte_identical_to_whole_file() -> None:
    assert se.structural_fingerprint(_BRANCH, None) == se.structural_fingerprint(_BRANCH)


def test_unparseable_baseline_falls_back_to_whole_file() -> None:
    """A diff against a file that will not parse would be a guess."""
    assert se.changed_hunks(_BRANCH, "def broken(:\n") is None
    assert se.structural_fingerprint(_BRANCH, "def broken(:\n") == se.structural_fingerprint(_BRANCH)


def test_unparseable_candidate_is_still_none() -> None:
    assert se.structural_fingerprint("def broken(:\n", _BASE) is None


def test_changed_hunks_are_the_new_side_of_the_diff_only() -> None:
    hunks = se.changed_hunks(_BRANCH, _BASE)
    assert hunks is not None and len(hunks) == 1
    assert "fallback" in hunks[0].dump
    assert "If" in hunks[0].kinds and "FunctionDef" in hunks[0].kinds
    assert se.changed_hunks(_BASE, _BASE) == ()


# --------------------------------------------------------------------------
# Threshold scaling by what the hunks are made of
# --------------------------------------------------------------------------


def test_flow_heavy_hunks_raise_the_bar() -> None:
    """Two rewrites of control flow must be NEARER identical to be 'the same'."""
    hunks = se.changed_hunks(_BRANCH, _BASE)
    assert se.hunk_threshold(0.95, hunks) == pytest.approx(0.97)


def test_plumbing_only_hunks_lower_the_bar() -> None:
    """A renamed constant is not a second algorithm."""
    hunks = se.changed_hunks(_BASE + "X = 1\n", _BASE)
    assert se.hunk_threshold(0.95, hunks) == pytest.approx(0.90)


def test_threshold_scaling_is_identity_without_hunks_and_clamped() -> None:
    assert se.hunk_threshold(0.95, None) == 0.95
    assert se.hunk_threshold(0.95, ()) == 0.95
    assert se.hunk_threshold(0.999, se.changed_hunks(_BRANCH, _BASE)) <= 1.0
    assert se.hunk_threshold(0.5, se.changed_hunks(_BASE + "X = 1\n", _BASE)) >= 0.5


def test_the_predicate_applies_the_scaled_threshold() -> None:
    """At 0.95, a pair at 0.96 is redundant whole-file but flow hunks push
    the bar to 0.97 -- the same pair, judged on its hunks, is distinct."""
    a = se.structural_fingerprint(_BRANCH, _BASE)
    # A near-twin differing by one branch node: similar, not identical.
    near = _BRANCH.replace("if kind == 'network':", "if kind in ('network', 'dns'):")
    b = se.structural_fingerprint(near, _BASE)
    sim = se.structural_similarity(a, b)
    assert 0.9 < sim < 1.0
    hunks = se.changed_hunks(near, _BASE)
    verdict_scaled, _ = se.is_structurally_redundant([b], [a], threshold=sim - 0.01, hunks=hunks)
    verdict_flat, _ = se.is_structurally_redundant([b], [a], threshold=sim - 0.01)
    # Same threshold input: flat says redundant (sim >= thr); scaled adds
    # +0.02 for flow hunks, so sim < thr+0.02 and the pair is distinct.
    assert verdict_flat is True and verdict_scaled is False


# --------------------------------------------------------------------------
# Baseline resolution and the candidate-list helpers
# --------------------------------------------------------------------------


def test_candidate_baseline_reads_the_file_the_candidate_replaces(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "mod.py").write_text(_BASE, encoding="utf-8")
    cand = {"file_path": "backend/mod.py", "full_content": _BRANCH}
    assert se.candidate_baseline(cand, str(tmp_path)) == _BASE
    assert se.candidate_baseline({"file_path": "backend/new.py"}, str(tmp_path)) is None
    assert se.candidate_baseline({"full_content": "x"}, str(tmp_path)) is None
    assert se.candidate_baseline("not a dict", str(tmp_path)) is None
    absolute = {"file_path": str(tmp_path / "backend" / "mod.py")}
    assert se.candidate_baseline(absolute, None) == _BASE


def test_fingerprint_candidates_uses_hunks_when_given_a_root(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "mod.py").write_text(_BASE, encoding="utf-8")
    cands = [
        {"candidate_hash": "a", "file_path": "backend/mod.py", "full_content": _BRANCH},
        {"candidate_hash": "b", "file_path": "backend/mod.py", "full_content": _TABLE},
    ]
    with_root = se.fingerprint_candidates(cands, str(tmp_path))
    without = se.fingerprint_candidates(cands)
    assert len(with_root) == 2 and len(without) == 2
    assert se.structural_similarity(*with_root) < 0.6
    assert se.structural_similarity(*without) > 0.95
    hunks = se.hunks_for_candidates(cands, str(tmp_path))
    assert len(hunks) == 3  # 1 statement in BRANCH + 2 in TABLE
    assert se.hunks_for_candidates(cands, None) == ()


def test_a_created_file_has_no_baseline_and_keeps_whole_file_behaviour(tmp_path: Path) -> None:
    cand = {"candidate_hash": "n", "file_path": "backend/brand_new.py", "full_content": _BRANCH}
    assert se.fingerprint_candidates([cand], str(tmp_path)) == (se.structural_fingerprint(_BRANCH),)
    assert se.hunks_for_candidates([cand], str(tmp_path)) == ()

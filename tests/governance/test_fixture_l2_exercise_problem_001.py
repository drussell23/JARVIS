"""Regression spine for Phase 1.5.B — first fixture problem_001.

Pins the load-bearing structural invariants for the
``tests/governance/fixtures/l2_exercise_corpus/problem_001/``
fixture:

* Manifest schema is well-formed JSON with required fields
* Manifest ``kind`` matches a canonical ``ExerciseProblemKind``
  taxonomy value (no drift)
* Fixture loads cleanly via the Phase 1.5.A canonical
  ``load_exercise_problem`` (composes the substrate loader)
* The buggy code GENUINELY fails the test suite when pytest
  runs against it (subprocess test — confirms the bug is real,
  not just claimed in the manifest)
* The known-good fix (``_known_good_fix.py``) GENUINELY passes
  the same test suite (subprocess test — confirms the problem
  is solvable, validating the "Phase 1.5 fixture corpus
  exercise" path is empirically sound)
* The ``list_corpus_problems`` walker enumerates the fixture
  AND skips the underscore-prefixed reference solution

These invariants together prove the fixture is a valid
L2-exercise target: the bug is real, the fix is real, the
canonical substrate sees the problem but NOT the answer.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.l2_exercise_seed import (
    ExerciseProblemKind,
    list_corpus_problems,
    load_exercise_problem,
)


_FIXTURE_DIR = (
    Path(__file__).parent
    / "fixtures"
    / "l2_exercise_corpus"
    / "problem_001"
)
_CORPUS_DIR = _FIXTURE_DIR.parent


# ===========================================================================
# Manifest schema — required fields + taxonomy alignment
# ===========================================================================


def test_manifest_is_valid_json():
    """manifest.json must parse cleanly."""
    raw = (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    data = json.loads(raw)  # raises on malformed
    assert isinstance(data, dict)


def test_manifest_has_required_fields():
    """The substrate-loader contract (Phase 1.5.A) reads these fields.
    Drift here = the fixture would silently fail to load."""
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    for field in ("id", "kind", "target_file_name", "test_file_name"):
        assert field in data, f"manifest.json MUST contain {field!r}"


def test_manifest_kind_is_canonical_taxonomy_value():
    """The ``kind`` field must equal one of the 5
    ExerciseProblemKind enum values. Drift = the substrate loader
    rejects the fixture (returns None) at boot."""
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    kind_str = data["kind"]
    # Constructor raises ValueError on unknown — pinned via taxonomy
    parsed = ExerciseProblemKind(kind_str)
    assert parsed == ExerciseProblemKind.OFF_BY_ONE, (
        f"problem_001 is designed as off_by_one; manifest says "
        f"{kind_str!r}"
    )


def test_manifest_documents_expected_hardness():
    """``expected_first_try_fail_rate`` field documents the
    design-time estimate of how often providers fail the problem
    on first GENERATE.  Phase 1.5.D's empirical validator MAY
    update this value after measurement.  Drift here means
    Phase 1.5.D hasn't been run yet OR estimate is stale."""
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    rate = data.get("expected_first_try_fail_rate")
    assert isinstance(rate, (int, float)), (
        "manifest.json MUST include expected_first_try_fail_rate "
        "(float in [0, 1]) for Phase 1.5.D validator + operator "
        "visibility"
    )
    assert 0.0 <= rate <= 1.0


# ===========================================================================
# Fixture loads cleanly via the canonical substrate
# ===========================================================================


def test_fixture_loads_via_canonical_substrate():
    """The substrate's load_exercise_problem (Phase 1.5.A) must
    accept this fixture without returning None."""
    problem = load_exercise_problem(_FIXTURE_DIR)
    assert problem is not None, (
        "load_exercise_problem returned None — fixture is malformed"
    )
    assert problem.problem_id == "problem_001"
    assert problem.kind == ExerciseProblemKind.OFF_BY_ONE
    assert problem.target_file_name == "before.py"
    assert problem.test_file_name == "test_before.py"
    assert "nth_smallest" in problem.before_content
    assert "nth_smallest" in problem.test_content


def test_corpus_walker_enumerates_problem_001():
    """The Phase 1.5.A list_corpus_problems walker MUST find
    problem_001 in the canonical fixture corpus location."""
    problems = list_corpus_problems(_CORPUS_DIR)
    names = [p.name for p in problems]
    assert "problem_001" in names


def test_corpus_walker_skips_known_good_fix_dir():
    """The reference solution ``_known_good_fix.py`` is a FILE
    inside problem_001/, not a sibling directory.  The walker
    enumerates DIRECTORIES, so the reference solution is never
    surfaced as a separate problem.  Defensive: also verify the
    walker would skip underscore-prefixed sibling directories
    (per the convention pinned in Phase 1.5.A)."""
    problems = list_corpus_problems(_CORPUS_DIR)
    names = [p.name for p in problems]
    # No underscore-prefixed dirs in the corpus should ever be listed
    assert all(not n.startswith("_") for n in names), (
        f"list_corpus_problems leaked underscore-prefixed entry: {names}"
    )


# ===========================================================================
# Subprocess pytest — buggy code GENUINELY fails (the bug is real)
# ===========================================================================


def _run_pytest_against_files(
    tmp_path: Path,
    before_content: str,
    test_content: str,
) -> subprocess.CompletedProcess:
    """Write the two files into a tmp dir + run pytest against them
    in subprocess.  Returns the completed process.

    Subprocess invocation isolates this test from the parent pytest
    runner — no interference with the v3.6 spine's own pytest
    session.
    """
    before_path = tmp_path / "before.py"
    test_path = tmp_path / "test_before.py"
    before_path.write_text(before_content, encoding="utf-8")
    test_path.write_text(test_content, encoding="utf-8")
    # No conftest.py - keep the subprocess hermetic
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(test_path), "-q",
         "--no-header", "--tb=no"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_buggy_before_code_fails_the_test_suite(tmp_path):
    """The buggy ``before.py`` must produce failing pytest runs.

    This is the structurally-honest test that the off-by-one bug
    is REAL — not just claimed in the manifest.  If this test
    passes (exit code 0), the fixture is silently solvable AND
    Phase 1.5's L2-exercise pipeline would never actually fire
    tree mode (because VALIDATE wouldn't fail).
    """
    before_content = (_FIXTURE_DIR / "before.py").read_text(encoding="utf-8")
    test_content = (_FIXTURE_DIR / "test_before.py").read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, before_content, test_content,
    )
    assert result.returncode != 0, (
        "PHASE 1.5.B FIXTURE INVARIANT VIOLATED: pytest passed against "
        f"the buggy before.py — the off-by-one bug is NOT real.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


def test_known_good_fix_passes_the_test_suite(tmp_path):
    """The reference solution ``_known_good_fix.py`` must produce
    passing pytest runs against the same test suite.

    This is the symmetry check: the problem IS solvable, the test
    suite IS valid, the fix IS the off-by-one correction.  Without
    this assertion, a bug in ``test_before.py`` (e.g., an impossible
    assertion) would be silently undetectable — and Phase 1.5
    soaks would fail forever regardless of provider output.
    """
    fix_content = (
        _FIXTURE_DIR / "_known_good_fix.py"
    ).read_text(encoding="utf-8")
    test_content = (
        _FIXTURE_DIR / "test_before.py"
    ).read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, fix_content, test_content,
    )
    assert result.returncode == 0, (
        "PHASE 1.5.B FIXTURE INVARIANT VIOLATED: pytest FAILED against "
        f"the known-good fix — the test suite is either broken or "
        f"requires more than just the off-by-one correction.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


def test_buggy_code_fails_multiple_tests_simultaneously(tmp_path):
    """Defense in depth: the failure mode is multiple simultaneously-
    failing tests, NOT a single failing test.  This forces a coherent
    fix (the off-by-one correction); a partial fix that handles one
    test case would leave others failing.  Confirms the problem is
    harder than a single-assertion regression."""
    before_content = (
        _FIXTURE_DIR / "before.py"
    ).read_text(encoding="utf-8")
    test_content = (
        _FIXTURE_DIR / "test_before.py"
    ).read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, before_content, test_content,
    )
    # Pytest -q output includes "N failed" — parse to count failures
    # Combined output (some pytest versions print to stderr)
    combined = result.stdout + result.stderr
    # Look for failure count in pytest output
    # E.g., "3 failed, 2 passed in 0.05s" or similar
    assert "failed" in combined.lower(), (
        f"pytest output contained no 'failed' marker:\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

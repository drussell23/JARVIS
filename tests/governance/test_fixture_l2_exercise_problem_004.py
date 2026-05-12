"""Regression spine for Phase 1.5.D.2 — fixture problem_004.

Pins the load-bearing structural invariants for the
``tests/governance/fixtures/l2_exercise_corpus/problem_004/``
fixture (recursive deep-merge with dict_keyerror semantic-edges
trap):

* Manifest schema well-formed JSON with required fields
* Manifest ``kind`` matches canonical ``ExerciseProblemKind``
  taxonomy value (``dict_keyerror``)
* Fixture loads cleanly via canonical Phase 1.5.A
  ``load_exercise_problem``
* Bug is REAL: pytest fails against ``before.py``
* Fix is REAL: pytest passes against ``_known_good_fix.py``
* Depth-ceiling canary 1: shallow ``dict.update`` style fix
  fails the two-level merge tests.
* Depth-ceiling canary 2: a level-1-only fix (handles nested
  dicts but doesn't recurse) fails the three-level merge tests.

The two canaries together prove the fixture distinguishes naive
single-level fixes (whether iterative or partially-deep) from a
fully-recursive merge.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from backend.core.ouroboros.governance.l2_exercise_seed import (
    ExerciseProblemKind,
    list_corpus_problems,
    load_exercise_problem,
)


_FIXTURE_DIR = (
    Path(__file__).parent
    / "fixtures"
    / "l2_exercise_corpus"
    / "problem_004"
)
_CORPUS_DIR = _FIXTURE_DIR.parent


# ===========================================================================
# Manifest schema
# ===========================================================================


def test_manifest_is_valid_json():
    raw = (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert isinstance(data, dict)


def test_manifest_has_required_fields():
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    for field in ("id", "kind", "target_file_name", "test_file_name"):
        assert field in data


def test_manifest_kind_is_canonical_dict_keyerror():
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    parsed = ExerciseProblemKind(data["kind"])
    assert parsed == ExerciseProblemKind.DICT_KEYERROR


def test_manifest_documents_expected_hardness():
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    rate = data.get("expected_first_try_fail_rate")
    assert isinstance(rate, (int, float))
    assert 0.0 <= rate <= 1.0


def test_manifest_documents_recursion_trap():
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    note = data.get("design_note", "").lower()
    assert "recurs" in note or "depth" in note, (
        f"design_note must surface recursion / depth-ceiling "
        f"rationale: {note!r}"
    )
    assert "trap_categories" in data
    assert isinstance(data["trap_categories"], list)


# ===========================================================================
# Canonical-substrate composition
# ===========================================================================


def test_fixture_loads_via_canonical_substrate():
    problem = load_exercise_problem(_FIXTURE_DIR)
    assert problem is not None
    assert problem.problem_id == "problem_004"
    assert problem.kind == ExerciseProblemKind.DICT_KEYERROR
    assert "def merge(" in problem.before_content
    assert "test_two_level_preserves_base_keys" in problem.test_content


def test_corpus_walker_enumerates_problem_004():
    problems = list_corpus_problems(_CORPUS_DIR)
    names = [p.name for p in problems]
    assert "problem_004" in names


def test_contract_surface_is_hidden():
    """v2-style discipline: function is called ``merge`` (not
    ``deep_merge``), docstring describes behavior generically
    (not by algorithm name).  The semantic edges (list-replace,
    scalar-vs-dict, recursion) must be inferred from test
    examples, not from name-readable contracts."""
    problem = load_exercise_problem(_FIXTURE_DIR)
    assert problem is not None
    forbidden = ["deep_merge", "DeepMerge", "recursive merge"]
    for phrase in forbidden:
        assert phrase not in problem.before_content, (
            f"before.py reintroduces telegraphing phrase {phrase!r}"
        )


# ===========================================================================
# Subprocess pytest — buggy code GENUINELY fails (multi-test)
# ===========================================================================


def _run_pytest_against_files(
    tmp_path: Path,
    before_content: str,
    test_content: str,
) -> subprocess.CompletedProcess:
    before_path = tmp_path / "before.py"
    test_path = tmp_path / "test_before.py"
    before_path.write_text(before_content, encoding="utf-8")
    test_path.write_text(test_content, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(test_path), "-q",
         "--no-header", "--tb=no"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_buggy_before_code_fails_the_test_suite(tmp_path):
    before_content = (_FIXTURE_DIR / "before.py").read_text(encoding="utf-8")
    test_content = (_FIXTURE_DIR / "test_before.py").read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, before_content, test_content,
    )
    assert result.returncode != 0, (
        "PHASE 1.5.D.2 FIXTURE INVARIANT VIOLATED: pytest passed "
        f"against the buggy before.py — the shallow-merge bug is "
        f"NOT real.\nstdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )


def test_known_good_fix_passes_the_test_suite(tmp_path):
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
        "PHASE 1.5.D.2 FIXTURE INVARIANT VIOLATED: pytest FAILED "
        f"against the known-good recursive merge.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


def test_buggy_code_fails_multiple_tests_simultaneously(tmp_path):
    before_content = (_FIXTURE_DIR / "before.py").read_text(encoding="utf-8")
    test_content = (_FIXTURE_DIR / "test_before.py").read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, before_content, test_content,
    )
    combined = result.stdout + result.stderr
    assert "failed" in combined.lower()


# ===========================================================================
# Naive-fix canary 1 — shallow merge fails depth-2
# ===========================================================================


_NAIVE_SHALLOW_FIX = '''"""Naive fix: shallow merge via dict-spread, no recursion.
This passes the flat tests but fails depth-2+ tests because
override's nested dict REPLACES base's nested dict at depth 2."""
from __future__ import annotations


def merge(base, override):
    # NAIVE: shallow merge via {**a, **b} — same flaw as dict.update.
    return {**base, **override}
'''


def test_naive_shallow_fix_fails_depth2_canary(tmp_path):
    """First depth-ceiling canary: shallow merge (dict-spread,
    update) MUST still fail at least one depth-2 test."""
    test_content = (_FIXTURE_DIR / "test_before.py").read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, _NAIVE_SHALLOW_FIX, test_content,
    )
    assert result.returncode != 0, (
        "DEPTH-CEILING TRAP NOT FUNCTIONING (shallow fix): pytest "
        "PASSED against the naive shallow-merge fix.  At least one "
        "two-level test should still fail.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert (
        "test_two_level_preserves_base_keys" in combined
        or "test_two_level_adds_new_nested_key" in combined
        or "test_two_level_disjoint_sibling_keys" in combined
    ), (
        f"Expected at least one two-level test to fail under "
        f"the shallow fix.\nstdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )


# ===========================================================================
# Naive-fix canary 2 — level-1-only fix fails depth-3
# ===========================================================================


_NAIVE_LEVEL1_FIX = '''"""Naive fix: handles nested dicts at level 1 but does NOT
recurse — so depth-3+ overrides still REPLACE wholesale.

Passes all depth-2 tests but fails depth-3+ stress tests."""
from __future__ import annotations


def merge(base, override):
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and k in result and isinstance(result[k], dict):
            # NAIVE: one level of dict-merging, then shallow copy of
            # the override value.  No recursion → depth-3 fails.
            merged = dict(result[k])
            merged.update(v)
            result[k] = merged
        else:
            result[k] = v
    return result
'''


def test_naive_level1_fix_fails_depth3_canary(tmp_path):
    """Second depth-ceiling canary: a level-1-deep fix that
    handles nested dicts at level 1 but does NOT recurse must
    still fail at the three-level test (where base + override
    have dicts at both level 1 AND level 2).

    Without this canary the fixture would silently allow
    partial-depth fixes — measuring nothing about recursion
    capability."""
    test_content = (_FIXTURE_DIR / "test_before.py").read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, _NAIVE_LEVEL1_FIX, test_content,
    )
    assert result.returncode != 0, (
        "DEPTH-CEILING TRAP NOT FUNCTIONING (level-1 fix): pytest "
        "PASSED against the level-1-only naive fix.  At least one "
        "three-level test should still fail.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert (
        "test_three_level_preserves_base_keys" in combined
        or "test_four_level_with_partial_overlap" in combined
    ), (
        f"Expected at least one depth>=3 test to fail under the "
        f"level-1 fix.\nstdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )

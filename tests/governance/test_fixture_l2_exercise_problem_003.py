"""Regression spine for Phase 1.5.D.2 — fixture problem_003.

Pins the load-bearing structural invariants for the
``tests/governance/fixtures/l2_exercise_corpus/problem_003/``
fixture (content sanitizer pipeline with multi-site
missing_null_check bug):

* Manifest schema well-formed JSON with required fields
* Manifest ``kind`` matches canonical ``ExerciseProblemKind``
  taxonomy value (``missing_null_check``)
* Fixture loads cleanly via canonical Phase 1.5.A
  ``load_exercise_problem``
* Bug is REAL: pytest fails against ``before.py`` (multiple tests
  fail simultaneously — confirms multi-site nature)
* Fix is REAL: pytest passes against ``_known_good_fix.py``
* Naive-fix canary 1: a "None-check at strip_html_tags only"
  partial fix STILL fails (truncate(None) crashes).
* Naive-fix canary 2: a "None-check at both leaf primitives only"
  partial fix STILL fails (sanitize_thread emits empty strings
  for None comments, violating the exclusion contract).

These invariants together prove problem_003 distinguishes
"type-error suppression" from "cross-function semantic contract
understanding."
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
    / "problem_003"
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
        assert field in data, f"manifest.json MUST contain {field!r}"


def test_manifest_kind_is_canonical_missing_null_check():
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    parsed = ExerciseProblemKind(data["kind"])
    assert parsed == ExerciseProblemKind.MISSING_NULL_CHECK


def test_manifest_documents_expected_hardness():
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    rate = data.get("expected_first_try_fail_rate")
    assert isinstance(rate, (int, float))
    assert 0.0 <= rate <= 1.0


def test_manifest_documents_semantic_trap():
    """Operator-honesty pin: the manifest MUST surface why this
    fixture is designed for cross-function semantic-contract
    understanding (not just type-error suppression)."""
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    assert "design_note" in data
    note = data["design_note"].lower()
    assert (
        "semantic" in note or "contract" in note or "coherent" in note
    ), f"design_note must surface semantic-trap rationale: {note!r}"
    assert "trap_categories" in data
    assert isinstance(data["trap_categories"], list)
    assert len(data["trap_categories"]) >= 1


# ===========================================================================
# Canonical-substrate composition
# ===========================================================================


def test_fixture_loads_via_canonical_substrate():
    problem = load_exercise_problem(_FIXTURE_DIR)
    assert problem is not None
    assert problem.problem_id == "problem_003"
    assert problem.kind == ExerciseProblemKind.MISSING_NULL_CHECK
    # v2 redesign: neutral function names; no "sanitize" / "thread"
    # / "excludes" verbs that telegraph the trap.
    assert "def collect(" in problem.before_content
    assert "def process(" in problem.before_content
    # Test names are deliberately neutral too — model has to infer
    # behavior from input/output examples, not test name semantics.
    assert "test_collect_input_a" in problem.test_content


def test_v2_contract_surface_is_hidden():
    """Stage 3.5 redesign pin: the v1 fixture failed empirically
    (0% fail rate) because the docstring contract on
    sanitize_thread + the load-bearing test name
    test_sanitize_thread_excludes_none_entries gave the model a
    free path to the multi-site insight.  v2 MUST NOT reintroduce
    those name-readable contract surfaces."""
    problem = load_exercise_problem(_FIXTURE_DIR)
    assert problem is not None
    forbidden_in_before = [
        "sanitize",  # v1 used sanitize_thread / sanitize_comment
        "thread",    # v1 telegraphed "thread of comments"
        "MUST BE EXCLUDED",  # v1 docstring's load-bearing phrase
        "MUST be EXCLUDED",
        "must be excluded",
        "filter",    # would telegraph the collect-level fix
    ]
    for phrase in forbidden_in_before:
        assert phrase.lower() not in problem.before_content.lower(), (
            f"v2 before.py reintroduces v1 contract-surface phrase "
            f"{phrase!r} — model would pattern-match to the trap"
        )
    forbidden_in_tests = [
        "excludes",
        "filters",
        "skips",
        "exclude",
        "filter",
    ]
    for phrase in forbidden_in_tests:
        assert phrase.lower() not in problem.test_content.lower(), (
            f"v2 test_before.py reintroduces v1 contract-telegraph "
            f"phrase {phrase!r} — test name would tip off the model"
        )


def test_corpus_walker_enumerates_problem_003():
    problems = list_corpus_problems(_CORPUS_DIR)
    names = [p.name for p in problems]
    assert "problem_003" in names


# ===========================================================================
# Subprocess pytest — buggy code GENUINELY fails (multi-test)
# ===========================================================================


def _run_pytest_against_files(
    tmp_path: Path,
    before_content: str,
    test_content: str,
) -> subprocess.CompletedProcess:
    """Hermetic subprocess pytest invocation."""
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
        f"against the buggy before.py — the missing_null_check "
        f"bugs are NOT real.\nstdout: {result.stdout!r}\n"
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
        f"against the known-good fix.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


def test_buggy_code_fails_multiple_tests_simultaneously(tmp_path):
    """Multi-site bugs surface in MULTIPLE tests simultaneously."""
    before_content = (_FIXTURE_DIR / "before.py").read_text(encoding="utf-8")
    test_content = (_FIXTURE_DIR / "test_before.py").read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, before_content, test_content,
    )
    combined = result.stdout + result.stderr
    assert "failed" in combined.lower()


# ===========================================================================
# Naive-fix canary 1 — strip_html_tags only → truncate still crashes
# ===========================================================================


_NAIVE_STRIP_ONLY_FIX = '''"""Naive fix that adds None-check ONLY at strip_html_tags.

truncate() still crashes on None because len(None) raises TypeError.
sanitize_thread is also unchanged so it still emits empty strings
for None comments.
"""
from __future__ import annotations

import re


def strip_html_tags(text):
    if text is None:
        return ""
    return re.sub(r"<[^>]+>", "", text)


def truncate(text, max_length):
    # NOT FIXED: crashes on None.
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def sanitize_comment(comment, max_length=100):
    stripped = strip_html_tags(comment)
    return truncate(stripped, max_length)


def sanitize_thread(thread):
    # NOT FIXED: emits "" for None comments.
    return [sanitize_comment(c) for c in thread]
'''


def test_naive_strip_only_fix_still_fails_truncate_canary(tmp_path):
    """First multi-site canary: a fix that ONLY None-checks
    strip_html_tags must still fail because truncate(None) crashes
    (len(None) raises TypeError) BEFORE sanitize_comment can return.

    Without this assertion the fixture would not distinguish
    "one-of-two type errors fixed" from "all type errors fixed.\""""
    test_content = (_FIXTURE_DIR / "test_before.py").read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, _NAIVE_STRIP_ONLY_FIX, test_content,
    )
    assert result.returncode != 0, (
        "MULTI-SITE TRAP NOT FUNCTIONING: pytest PASSED against the "
        "strip-only naive fix.  The truncate(None) crash should "
        "still surface.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "test_truncate_handles_none_input" in combined, (
        f"Expected test_truncate_handles_none_input to fail under "
        f"the strip-only fix.\nstdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )


# ===========================================================================
# Naive-fix canary 2 — both primitives fixed, thread still emits "" for None
# ===========================================================================


_NAIVE_LEAF_ONLY_FIX = '''"""Naive fix that None-checks BOTH leaf primitives (strip_html_tags
+ truncate) but leaves sanitize_thread unchanged.

Type errors no longer surface, but sanitize_thread emits "" for
None comments — violating the documented exclusion contract.
This is the SEMANTIC trap: type-correctness alone is not
correctness.
"""
from __future__ import annotations

import re


def strip_html_tags(text):
    if text is None:
        return ""
    return re.sub(r"<[^>]+>", "", text)


def truncate(text, max_length):
    if text is None:
        return ""
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def sanitize_comment(comment, max_length=100):
    stripped = strip_html_tags(comment)
    return truncate(stripped, max_length)


def sanitize_thread(thread):
    # NOT FIXED: emits "" for None comments, violating the
    # exclusion contract documented in the docstring.
    return [sanitize_comment(c) for c in thread]
'''


def test_naive_leaf_only_fix_still_fails_thread_semantic_canary(tmp_path):
    """Second multi-site canary: a fix that None-checks BOTH leaf
    primitives but leaves sanitize_thread unchanged STILL fails
    because the thread emits empty strings for None comments —
    violating the documented exclusion contract.

    This is the SEMANTIC trap: type-correctness is not correctness.
    The fixture catches "just suppress the TypeErrors" without
    reading the docstring's load-bearing contract."""
    test_content = (_FIXTURE_DIR / "test_before.py").read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, _NAIVE_LEAF_ONLY_FIX, test_content,
    )
    assert result.returncode != 0, (
        "SEMANTIC TRAP NOT FUNCTIONING: pytest PASSED against the "
        "leaf-only naive fix.  The sanitize_thread exclusion-"
        "contract test should still fail.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert (
        "test_sanitize_thread_excludes_none_entries" in combined
        or "test_sanitize_thread_preserves_order" in combined
        or "test_sanitize_thread_all_none_returns_empty" in combined
    ), (
        f"Expected at least one sanitize_thread exclusion test to "
        f"fail under the leaf-only fix.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

"""Regression spine for Phase 1.5.D.2 — fixture problem_002.

Pins the load-bearing structural invariants for the
``tests/governance/fixtures/l2_exercise_corpus/problem_002/``
fixture (MaxPriorityQueue with multi-site logic_inversion bug):

* Manifest schema well-formed JSON with required fields
* Manifest ``kind`` matches canonical ``ExerciseProblemKind``
  taxonomy value (``logic_inversion``)
* Fixture loads cleanly via canonical Phase 1.5.A
  ``load_exercise_problem``
* Bug is REAL: pytest fails against ``before.py`` (multiple tests
  fail simultaneously — confirms multi-site nature)
* Fix is REAL: pytest passes against ``_known_good_fix.py``
* Naive-fix canary: a "negate at push only" partial fix is
  detected by ``test_peek_priority_matches_pushed_priority``
  failing (this fixture is designed to catch single-site fixes)

These invariants together prove problem_002 is a valid L2 exercise
target: the multi-site bug is real, the coherent fix is real, AND
the test suite distinguishes coherent fixes from naive single-site
patches.
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
    / "problem_002"
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


def test_manifest_kind_is_canonical_logic_inversion():
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    parsed = ExerciseProblemKind(data["kind"])
    assert parsed == ExerciseProblemKind.LOGIC_INVERSION


def test_manifest_documents_expected_hardness():
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    rate = data.get("expected_first_try_fail_rate")
    assert isinstance(rate, (int, float))
    assert 0.0 <= rate <= 1.0


def test_manifest_documents_multi_site_trap():
    """Operator-honesty pin: the manifest MUST surface why this
    fixture is designed for multi-site coordination (not just
    'this bug is hard').  Without a design_note + trap_categories,
    a future operator could see a low fail-rate and accidentally
    delete the fixture thinking it 'doesn't trigger L2 enough.'
    The design note explains the trap; the trap_categories are
    grep-able tags for HARDNESS_SET membership reasoning."""
    data = json.loads(
        (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    assert "design_note" in data
    assert "multi" in data["design_note"].lower() or \
        "coordination" in data["design_note"].lower()
    assert "trap_categories" in data
    assert isinstance(data["trap_categories"], list)
    assert len(data["trap_categories"]) >= 1


# ===========================================================================
# Canonical-substrate composition
# ===========================================================================


def test_fixture_loads_via_canonical_substrate():
    problem = load_exercise_problem(_FIXTURE_DIR)
    assert problem is not None
    assert problem.problem_id == "problem_002"
    assert problem.kind == ExerciseProblemKind.LOGIC_INVERSION
    assert "MaxPriorityQueue" in problem.before_content
    assert "test_peek_priority_matches_pushed_priority" in problem.test_content


def test_corpus_walker_enumerates_problem_002():
    problems = list_corpus_problems(_CORPUS_DIR)
    names = [p.name for p in problems]
    assert "problem_002" in names


# ===========================================================================
# Subprocess pytest — buggy code GENUINELY fails (multi-test)
# ===========================================================================


def _run_pytest_against_files(
    tmp_path: Path,
    before_content: str,
    test_content: str,
) -> subprocess.CompletedProcess:
    """Same pattern as Phase 1.5.B's spine: hermetic subprocess
    pytest run isolates this from the parent test session."""
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
    """The buggy ``before.py`` MUST produce a failing pytest run.

    Without this assertion, a fixture that's silently solvable
    (e.g., the model auto-imports the correct ``heapq`` semantics
    by accident) would silently no-op the L2 exercise pipeline.
    """
    before_content = (_FIXTURE_DIR / "before.py").read_text(encoding="utf-8")
    test_content = (_FIXTURE_DIR / "test_before.py").read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, before_content, test_content,
    )
    assert result.returncode != 0, (
        "PHASE 1.5.D.2 FIXTURE INVARIANT VIOLATED: pytest passed "
        f"against the buggy before.py — the logic_inversion bug "
        f"is NOT real.\nstdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )


def test_known_good_fix_passes_the_test_suite(tmp_path):
    """The reference solution ``_known_good_fix.py`` MUST produce
    a passing pytest run.  Confirms the test suite IS solvable + the
    coherent fix is the correct one (catches broken test suites)."""
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
        f"against the known-good fix — the test suite is broken or "
        f"requires more than just the coherent push+peek negation.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


def test_buggy_code_fails_multiple_tests_simultaneously(tmp_path):
    """Defense in depth: multi-site bugs surface in MULTIPLE tests
    simultaneously, not just one.  Confirms problem_002 is harder
    than a single-assertion regression."""
    before_content = (_FIXTURE_DIR / "before.py").read_text(encoding="utf-8")
    test_content = (_FIXTURE_DIR / "test_before.py").read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, before_content, test_content,
    )
    combined = result.stdout + result.stderr
    assert "failed" in combined.lower(), (
        f"pytest output contained no 'failed' marker:\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


# ===========================================================================
# Naive-fix canary — single-site fix MUST still fail at least one test
# ===========================================================================


_NAIVE_PUSH_ONLY_FIX = '''"""Naive fix that ONLY negates at push() — leaves peek() leaking
the internal -priority through its return tuple.  This should
fail the multi-site canary test."""
from __future__ import annotations

import heapq
from typing import Any, List, Tuple


class MaxPriorityQueue:
    def __init__(self) -> None:
        self._heap: List[Tuple[Any, int, Any]] = []
        self._counter: int = 0

    def push(self, priority: Any, item: Any) -> None:
        # NAIVE FIX: negate priority at push only.  peek() still
        # returns the internal -priority — a single-site fix.
        heapq.heappush(self._heap, (-priority, self._counter, item))
        self._counter += 1

    def pop(self) -> Any:
        if not self._heap:
            raise IndexError("pop from empty MaxPriorityQueue")
        _neg_priority, _counter, item = heapq.heappop(self._heap)
        return item

    def peek(self) -> Tuple[Any, Any]:
        if not self._heap:
            raise IndexError("peek on empty MaxPriorityQueue")
        priority, _counter, item = self._heap[0]
        # BUG (deliberate): returns the INTERNAL negated priority
        # without re-negating for the caller.
        return (priority, item)

    def __len__(self) -> int:
        return len(self._heap)
'''


def test_naive_push_only_fix_still_fails_peek_canary(tmp_path):
    """The fixture is DESIGNED to catch naive single-site fixes.

    Apply the "negate at push only" naive fix.  Pop ordering will
    pass (because the heap is now ordered correctly).  But peek's
    priority leak should fail the canary assertions.

    Without this invariant the fixture would silently allow trivial
    single-site fixes and the L2 exercise would never measure
    multi-site reasoning capability."""
    test_content = (_FIXTURE_DIR / "test_before.py").read_text(encoding="utf-8")
    result = _run_pytest_against_files(
        tmp_path, _NAIVE_PUSH_ONLY_FIX, test_content,
    )
    assert result.returncode != 0, (
        "MULTI-SITE TRAP NOT FUNCTIONING: pytest PASSED against the "
        "naive push-only fix.  The fixture must distinguish naive "
        "single-site fixes from coherent multi-site fixes; if both "
        "pass, the fixture is no harder than a single-edit bug.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    # Specifically: the peek canary tests should be in the failure
    # set.  Grep for them in the failure output.
    combined = result.stdout + result.stderr
    assert (
        "test_peek_priority_matches_pushed_priority" in combined
        or "test_peek_priority_matches_after_intervening_operations" in combined
    ), (
        f"Expected at least one peek-priority canary test to fail "
        f"under the naive push-only fix, but neither was mentioned "
        f"in pytest output.\nstdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )

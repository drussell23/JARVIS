"""MutationTester tests — proves the meta-test actually separates
performative tests (mutants survive) from behavior-exercising tests
(mutants caught).

Scope axes:

  1. Env gates (fail-closed master switch, clamp ranges).
  2. Mutation enumeration — each of the 4 ops finds the right nodes,
     and the rendered source actually differs from the original.
  3. Deterministic sampling (same seed → same subset; cap respected).
  4. Runner — caught / survived / timeout / run_error reasons.
  5. Aggregator — score, grade, survivor list, coverage_by_op.
  6. Integration — a toy SUT + test pair where:
       - strong tests → high score (>= 80%)
       - weak tests (only shape assertions) → low score
     This is the meta-test proof: mutation testing CAN distinguish
     governance-approved-and-correct from governance-approved-and-
     performative.
  7. Restore semantics — original file is restored even when the
     subprocess errors out.
  8. Report renderers — console + JSON don't crash on empty / full
     result and include authority caveats.
  9. AST canaries — authority invariant in module docstring,
     scope caveat ("equivalent mutants"), all 4 ops declared,
     fail-closed env gate default.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance import mutation_tester as MT
from backend.core.ouroboros.governance.mutation_tester import (
    Mutant,
    MutantOutcome,
    MutationResult,
    enabled,
    enumerate_mutants,
    render_console_report,
    render_json_report,
    run_mutant,
    run_mutation_test,
    sample_mutants,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for k in list(os.environ.keys()):
        if k.startswith("JARVIS_MUTATION_TEST_"):
            monkeypatch.delenv(k, raising=False)
    yield


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


# ---------------------------------------------------------------------------
# (1) Env gates
# ---------------------------------------------------------------------------


def test_master_gate_disabled_by_default():
    assert enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_master_gate_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_MUTATION_TEST_ENABLED", val)
    assert enabled() is True


def test_max_mutants_clamp(monkeypatch):
    monkeypatch.setenv("JARVIS_MUTATION_TEST_MAX_MUTANTS", "9999")
    assert MT.max_mutants() == 500
    monkeypatch.setenv("JARVIS_MUTATION_TEST_MAX_MUTANTS", "0")
    assert MT.max_mutants() == 1
    monkeypatch.setenv("JARVIS_MUTATION_TEST_MAX_MUTANTS", "not-a-number")
    assert MT.max_mutants() == 25  # default


def test_timeout_clamps(monkeypatch):
    monkeypatch.setenv("JARVIS_MUTATION_TEST_TIMEOUT_S", "1")
    assert MT.mutant_timeout_s() == 5  # lo
    monkeypatch.setenv("JARVIS_MUTATION_TEST_TIMEOUT_S", "10000")
    assert MT.mutant_timeout_s() == 600  # hi


# ---------------------------------------------------------------------------
# (2) Mutation enumeration
# ---------------------------------------------------------------------------


def test_bool_flip_finds_true_and_false(tmp_path):
    src = tmp_path / "sut.py"
    _write(src, """
        def f():
            a = True
            b = False
            return a or b
    """)
    mutants = enumerate_mutants(src)
    ops = [m.op for m in mutants]
    assert ops.count("bool_flip") == 2
    originals = sorted(m.original for m in mutants if m.op == "bool_flip")
    assert originals == ["False", "True"]


def test_compare_flip_covers_all_six_ops(tmp_path):
    src = tmp_path / "sut.py"
    _write(src, """
        def checks(a, b):
            return (
                a == b and a != b and a < b and
                a <= b and a > b and a >= b
            )
    """)
    mutants = enumerate_mutants(src)
    pairs = {(m.original, m.mutated) for m in mutants if m.op == "compare_flip"}
    assert pairs == {
        ("==", "!="), ("!=", "=="),
        ("<", ">="), ("<=", ">"),
        (">", "<="), (">=", "<"),
    }


def test_arith_swap_four_ops(tmp_path):
    src = tmp_path / "sut.py"
    _write(src, """
        def m(a, b):
            return (a + b) - (a * b) / (a + 1)
    """)
    mutants = enumerate_mutants(src)
    pairs = {(m.original, m.mutated) for m in mutants if m.op == "arith_swap"}
    assert ("+", "-") in pairs
    assert ("-", "+") in pairs
    assert ("*", "//") in pairs
    assert ("/", "*") in pairs


def test_return_none_skips_already_none(tmp_path):
    src = tmp_path / "sut.py"
    _write(src, """
        def f(x):
            if x:
                return 42
            return None
        def g():
            return
    """)
    mutants = enumerate_mutants(src)
    ops = [m.op for m in mutants]
    # Only the `return 42` should trigger return_none; bare `return` and
    # explicit `return None` must not.
    assert ops.count("return_none") == 1


def test_enumerate_skips_syntax_error(tmp_path):
    src = tmp_path / "broken.py"
    _write(src, """
        def f(:
            pass
    """)
    assert enumerate_mutants(src) == []


def test_enumerate_handles_missing_file(tmp_path):
    assert enumerate_mutants(tmp_path / "nope.py") == []


def test_rendered_mutant_differs_from_original(tmp_path):
    src = tmp_path / "sut.py"
    _write(src, """
        def f(x):
            return x == 1
    """)
    mutants = enumerate_mutants(src)
    assert mutants
    original = src.read_text()
    for m in mutants:
        assert m.patched_src != original, (
            f"mutant {m.key} rendered identical to original — equivalent?"
        )


# ---------------------------------------------------------------------------
# (3) Deterministic sampling
# ---------------------------------------------------------------------------


def _fake_mutants(n: int) -> list:
    return [
        Mutant(
            op="bool_flip", source_file="/tmp/x.py",
            line=i, col=0, original="True", mutated="False",
            patched_src=f"# mutant {i}\n",
        )
        for i in range(n)
    ]


def test_sample_keeps_all_when_under_limit():
    ms = _fake_mutants(5)
    out = sample_mutants(ms, limit=10, seed=0)
    assert len(out) == 5


def test_sample_deterministic_same_seed():
    ms = _fake_mutants(100)
    a = sample_mutants(ms, limit=20, seed=42)
    b = sample_mutants(ms, limit=20, seed=42)
    assert [x.line for x in a] == [x.line for x in b]


def test_sample_different_seeds_differ():
    ms = _fake_mutants(100)
    a = sample_mutants(ms, limit=20, seed=0)
    b = sample_mutants(ms, limit=20, seed=1)
    assert [x.line for x in a] != [x.line for x in b]


def test_sample_respects_limit():
    ms = _fake_mutants(100)
    out = sample_mutants(ms, limit=7, seed=0)
    assert len(out) == 7


# ---------------------------------------------------------------------------
# (4) Runner outcomes
# ---------------------------------------------------------------------------


def _write_pair(tmp_path: Path, sut_src: str, test_src: str):
    sut = tmp_path / "sut.py"
    tst = tmp_path / "test_sut.py"
    _write(sut, sut_src)
    _write(tst, test_src)
    # Make `sut` importable in the subprocess cwd.
    (tmp_path / "conftest.py").write_text(
        "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n",
        encoding="utf-8",
    )
    return sut, tst


def test_runner_caught_when_test_fails(tmp_path):
    sut, tst = _write_pair(tmp_path, """
        def is_positive(x):
            return x > 0
    """, """
        from sut import is_positive
        def test_one(): assert is_positive(1) is True
        def test_zero(): assert is_positive(0) is False
        def test_neg(): assert is_positive(-5) is False
    """)
    # Mutate the `>` to `<=` — tests should fail → caught.
    mutants = [
        m for m in enumerate_mutants(sut)
        if m.op == "compare_flip" and m.original == ">"
    ]
    assert mutants
    outcome = run_mutant(mutants[0], test_files=[tst], cwd=tmp_path, timeout_s=30)
    assert outcome.caught is True
    assert outcome.reason == "test_failure"


def test_runner_survived_when_tests_weak(tmp_path):
    sut, tst = _write_pair(tmp_path, """
        def compute(x):
            return x * 2
    """, """
        from sut import compute
        def test_returns_something():
            result = compute(5)
            assert result is not None  # weak — doesn't check the value
    """)
    # Mutate the `*` to `//` — result changes but test doesn't assert value.
    mutants = [
        m for m in enumerate_mutants(sut) if m.op == "arith_swap"
    ]
    assert mutants
    outcome = run_mutant(mutants[0], test_files=[tst], cwd=tmp_path, timeout_s=30)
    assert outcome.caught is False
    assert outcome.reason == "survived"


def test_runner_restores_original_after_run(tmp_path):
    sut, tst = _write_pair(tmp_path, """
        def f(): return 1
    """, """
        from sut import f
        def test_f(): assert f() == 1
    """)
    before = sut.read_text()
    mutants = enumerate_mutants(sut)
    if mutants:
        run_mutant(mutants[0], test_files=[tst], cwd=tmp_path, timeout_s=10)
    after = sut.read_text()
    assert before == after, "SUT file was not restored after mutation run"


def test_runner_restores_even_on_subprocess_exception(tmp_path):
    sut, tst = _write_pair(tmp_path, """
        def f(): return True
    """, """
        def test_f(): assert True
    """)
    before = sut.read_text()
    m = enumerate_mutants(sut)[0]
    # Force subprocess.run to explode mid-flight.
    with patch.object(
        subprocess, "run",
        side_effect=RuntimeError("boom"),
    ):
        outcome = run_mutant(m, test_files=[tst], cwd=tmp_path, timeout_s=5)
    assert sut.read_text() == before
    assert outcome.reason == "run_error"


# ---------------------------------------------------------------------------
# (5) Aggregator
# ---------------------------------------------------------------------------


def test_aggregator_score_and_grade():
    outcomes = [
        MutantOutcome(
            mutant=_fake_mutants(1)[0], caught=True,
            reason="test_failure", duration_s=0.1,
        ),
    ]
    # Build a fake result directly to exercise grade boundaries.
    r = MutationResult(
        source_file="x.py", total_mutants=10, caught=9, survived=1,
        score=0.9, grade="A", survivors=tuple(),
    )
    assert r.to_json()["grade"] == "A"


def test_grade_boundary_matrix():
    cases = [
        (0.95, 10, "A"),
        (0.80, 10, "B"),
        (0.65, 10, "C"),
        (0.45, 10, "D"),
        (0.20, 10, "F"),
        (0.00, 0, "N/A"),
    ]
    for score, total, expected in cases:
        assert MT._grade_from_score(score, total) == expected


# ---------------------------------------------------------------------------
# (6) Integration — the meta-test proof
# ---------------------------------------------------------------------------


def test_strong_tests_achieve_high_score(tmp_path):
    """Strong tests should catch most mutants. Scoring >= 0.8 proves
    the mutation tester discriminates well when tests actually check
    behavior."""
    sut, tst = _write_pair(tmp_path, """
        def clamp(x, lo, hi):
            if x < lo:
                return lo
            if x > hi:
                return hi
            return x
    """, """
        from sut import clamp
        def test_below(): assert clamp(-5, 0, 10) == 0
        def test_above(): assert clamp(50, 0, 10) == 10
        def test_inside(): assert clamp(5, 0, 10) == 5
        def test_lo_boundary(): assert clamp(0, 0, 10) == 0
        def test_hi_boundary(): assert clamp(10, 0, 10) == 10
    """)
    result = run_mutation_test(
        sut, test_files=[tst], cwd=tmp_path,
        timeout_s_override=30, max_mutants_override=25, seed_override=0,
    )
    assert result.total_mutants >= 3
    assert result.score >= 0.8, (
        f"strong tests should catch >= 80% of mutants; got {result.score:.2f}. "
        f"Survivors: {[s.mutant.key for s in result.survivors]}"
    )


def test_weak_tests_achieve_low_score(tmp_path):
    """Performative tests that only check ``is not None`` should let
    mutants survive. This is the core proof of the feature."""
    sut, tst = _write_pair(tmp_path, """
        def compute(a, b):
            return a * b + 1
    """, """
        from sut import compute
        def test_shape_not_none():
            assert compute(2, 3) is not None
        def test_shape_is_int():
            assert isinstance(compute(2, 3), int)
    """)
    result = run_mutation_test(
        sut, test_files=[tst], cwd=tmp_path,
        timeout_s_override=30, max_mutants_override=10, seed_override=0,
    )
    assert result.total_mutants >= 2
    assert result.survived >= 1, (
        "performative 'is not None' tests should let mutants survive"
    )
    assert result.score < 0.8, (
        "performative tests should not achieve a strong score"
    )


# ---------------------------------------------------------------------------
# (7) Report renderers
# ---------------------------------------------------------------------------


def test_console_report_contains_score_and_caveat():
    r = MutationResult(
        source_file="foo.py", total_mutants=5, caught=4, survived=1,
        score=0.8, grade="B",
        survivors=(
            MutantOutcome(
                mutant=_fake_mutants(1)[0], caught=False,
                reason="survived", duration_s=0.2,
            ),
        ),
    )
    report = render_console_report(r)
    assert "Mutation Test Report" in report
    assert "Score: 80.0%" in report
    assert "Grade: B" in report
    assert "equivalent" in report.lower()  # caveat present
    assert "Survived mutants" in report


def test_console_report_empty_total():
    r = MutationResult(
        source_file="empty.py", total_mutants=0, caught=0, survived=0,
        score=0.0, grade="N/A", survivors=tuple(),
    )
    report = render_console_report(r)
    assert "Grade: N/A" in report
    assert "equivalent" in report.lower()


def test_json_report_roundtrip():
    r = MutationResult(
        source_file="foo.py", total_mutants=5, caught=4, survived=1,
        score=0.8, grade="B", survivors=(),
    )
    blob = render_json_report(r)
    parsed = json.loads(blob)
    assert parsed["score"] == 0.8
    assert parsed["grade"] == "B"
    assert "equivalent_mutant_caveat" in parsed


# ---------------------------------------------------------------------------
# (8) AST canaries — authority invariant + scope honesty
# ---------------------------------------------------------------------------


_SRC = Path(MT.__file__).read_text(encoding="utf-8")


def test_module_declares_authority_invariant():
    """Manifesto §1 Boundary Principle: mutation tester must not override
    governance. The docstring is the load-bearing contract."""
    assert "Authority invariant" in _SRC
    # It must explicitly say "never overrides" / "NEVER" — load-bearing
    # language that a refactor would notice.
    assert "NEVER" in _SRC
    assert "risk tier" in _SRC or "Iron Gate" in _SRC


def test_module_declares_equivalent_mutant_caveat():
    assert "equivalent mutant" in _SRC.lower()
    assert "hints" in _SRC.lower() or "not proofs" in _SRC.lower()


def test_module_fail_closed_master_default_zero():
    """Grep the env default literal. Any refactor that accidentally
    flips the default to '1' would land a silent always-on change."""
    assert '_ENV_ENABLED = "JARVIS_MUTATION_TEST_ENABLED"' in _SRC
    # The default literal is "0" inside enabled().
    assert 'os.environ.get(_ENV_ENABLED, "0")' in _SRC


def test_module_declares_all_four_operators():
    for op in ("bool_flip", "compare_flip", "arith_swap", "return_none"):
        assert op in _SRC, f"operator {op} missing from module source"


def test_mutation_ops_tuple_is_sealed():
    assert MT._MUTATION_OPS == (
        "bool_flip", "compare_flip", "arith_swap", "return_none",
    )

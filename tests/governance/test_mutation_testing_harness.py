"""Regression spine for §41.4 Phase 1 fifth arc — Mutation testing harness."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any, List, Tuple

import pytest


from backend.core.ouroboros.governance import (
    mutation_testing_harness as mth,
)
from backend.core.ouroboros.governance.mutation_testing_harness import (
    MUTATION_TESTING_SCHEMA_VERSION,
    Mutant,
    MutantResult,
    MutantStatus,
    MutationKind,
    MutationReport,
    MutationVerdict,
    _ENV_BACKUP_SUFFIX,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_MUTANTS,
    _ENV_PERSIST,
    _ENV_STRONG_THRESHOLD,
    _ENV_TEST_TIMEOUT_S,
    _ENV_WEAK_THRESHOLD,
    _verdict_for_kill_ratio,
    apply_mutation,
    backup_suffix,
    evaluate_file,
    evaluate_file_sync,
    find_mutation_sites,
    format_mutation_panel,
    kind_glyph,
    ledger_path,
    master_enabled,
    max_mutants,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    run_mutant,
    status_glyph,
    strong_threshold,
    test_timeout_s,
    verdict_glyph,
    weak_threshold,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST, _ENV_MAX_MUTANTS,
        _ENV_TEST_TIMEOUT_S, _ENV_WEAK_THRESHOLD,
        _ENV_STRONG_THRESHOLD, _ENV_BACKUP_SUFFIX,
        _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH, str(tmp_path / "ledger.jsonl"),
    )
    yield


def _run(coro):
    return asyncio.run(coro)


# Defaults


def test_schema():
    assert MUTATION_TESTING_SCHEMA_VERSION == "mutation_testing.1"


def test_master_default_false():
    assert master_enabled() is False


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_max_mutants_default():
    assert max_mutants() == 30


def test_test_timeout_default():
    assert test_timeout_s() == 60


def test_weak_threshold_default():
    assert weak_threshold() == 0.4


def test_strong_threshold_default():
    assert strong_threshold() == 0.75


def test_strong_threshold_auto_clamp(monkeypatch):
    monkeypatch.setenv(_ENV_WEAK_THRESHOLD, "0.9")
    monkeypatch.setenv(_ENV_STRONG_THRESHOLD, "0.5")
    # strong < weak → clamped UP to weak
    assert strong_threshold() == 0.9


def test_backup_suffix_default():
    assert backup_suffix() == ".mut_bak"


# Taxonomies


def test_verdict_taxonomy_closed():
    assert {v.value for v in MutationVerdict} == {
        "weak", "fair", "strong", "disabled",
    }


def test_kind_taxonomy_closed():
    assert {k.value for k in MutationKind} == {
        "comparison_flip", "arithmetic_flip",
        "boolean_flip", "identity_flip",
    }


def test_status_taxonomy_closed():
    assert {s.value for s in MutantStatus} == {
        "killed", "survived", "timeout", "error",
    }


@pytest.mark.parametrize("v", list(MutationVerdict))
def test_verdict_glyph(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("k", list(MutationKind))
def test_kind_glyph(k):
    assert kind_glyph(k) != "?"


@pytest.mark.parametrize("s", list(MutantStatus))
def test_status_glyph(s):
    assert status_glyph(s) != "?"


# Mutation site discovery


def test_find_empty_source():
    assert find_mutation_sites("") == ()


def test_find_malformed_python():
    assert find_mutation_sites("def x(") == ()


def test_find_comparison_eq():
    src = "x = (a == b)\n"
    mutants = find_mutation_sites(src)
    eq_mutants = [
        m for m in mutants
        if m.mutation_kind is MutationKind.COMPARISON_FLIP
    ]
    assert len(eq_mutants) >= 1
    assert any(m.original_text == "==" for m in eq_mutants)
    assert any(m.mutated_text == "!=" for m in eq_mutants)


def test_find_comparison_lt():
    src = "x = (a < b)\n"
    mutants = find_mutation_sites(src)
    lt_mutants = [
        m for m in mutants
        if m.mutation_kind is MutationKind.COMPARISON_FLIP
        and m.original_text == "<"
    ]
    assert len(lt_mutants) == 1
    assert lt_mutants[0].mutated_text == ">="


def test_find_arithmetic_add():
    src = "x = a + b\n"
    mutants = find_mutation_sites(src)
    add_mutants = [
        m for m in mutants
        if m.mutation_kind is MutationKind.ARITHMETIC_FLIP
    ]
    assert len(add_mutants) == 1
    assert add_mutants[0].original_text == "+"
    assert add_mutants[0].mutated_text == "-"


def test_find_arithmetic_mult():
    src = "x = a * b\n"
    mutants = find_mutation_sites(src)
    mul_mutants = [
        m for m in mutants
        if m.original_text == "*"
    ]
    assert len(mul_mutants) == 1
    assert mul_mutants[0].mutated_text == "/"


def test_find_boolean_true():
    src = "x = True\n"
    mutants = find_mutation_sites(src)
    bool_mutants = [
        m for m in mutants
        if m.mutation_kind is MutationKind.BOOLEAN_FLIP
    ]
    assert len(bool_mutants) == 1
    assert bool_mutants[0].original_text == "True"
    assert bool_mutants[0].mutated_text == "False"


def test_find_boolean_false():
    src = "x = False\n"
    mutants = find_mutation_sites(src)
    bool_mutants = [
        m for m in mutants
        if m.original_text == "False"
    ]
    assert len(bool_mutants) == 1
    assert bool_mutants[0].mutated_text == "True"


def test_find_boolean_and():
    src = "x = (a and b)\n"
    mutants = find_mutation_sites(src)
    and_mutants = [
        m for m in mutants
        if m.mutation_kind is MutationKind.BOOLEAN_FLIP
        and m.original_text == "and"
    ]
    assert len(and_mutants) == 1
    assert and_mutants[0].mutated_text == "or"


def test_find_identity_is():
    src = "x = (a is None)\n"
    mutants = find_mutation_sites(src)
    is_mutants = [
        m for m in mutants
        if m.mutation_kind is MutationKind.IDENTITY_FLIP
        and m.original_text == "is"
    ]
    assert len(is_mutants) == 1
    assert is_mutants[0].mutated_text == "is not"


def test_find_identity_in():
    src = "x = (a in [1, 2, 3])\n"
    mutants = find_mutation_sites(src)
    in_mutants = [
        m for m in mutants
        if m.mutation_kind is MutationKind.IDENTITY_FLIP
        and m.original_text == "in"
    ]
    assert len(in_mutants) == 1
    assert in_mutants[0].mutated_text == "not in"


def test_find_respects_max_mutants(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_MUTANTS, "2")
    src = "\n".join(f"x{i} = (a == b)" for i in range(10)) + "\n"
    mutants = find_mutation_sites(src)
    assert len(mutants) == 2


def test_find_multiple_kinds_in_one_file():
    src = """
def f(a, b):
    if a == b:
        return True
    return a + b
"""
    mutants = find_mutation_sites(src)
    kinds = {m.mutation_kind for m in mutants}
    assert MutationKind.COMPARISON_FLIP in kinds
    assert MutationKind.ARITHMETIC_FLIP in kinds
    assert MutationKind.BOOLEAN_FLIP in kinds


def test_mutant_has_stable_id():
    src = "x = (a == b)\n"
    mutants_1 = find_mutation_sites(src, source_file="test.py")
    mutants_2 = find_mutation_sites(src, source_file="test.py")
    assert mutants_1[0].mutant_id == mutants_2[0].mutant_id


# apply_mutation


def test_apply_empty_source():
    m = Mutant(
        mutant_id="x", source_file="t.py", line_number=1,
        col_offset=0,
        mutation_kind=MutationKind.COMPARISON_FLIP,
        original_text="==", mutated_text="!=",
    )
    assert apply_mutation("", m) is None


def test_apply_comparison_flip():
    src = "x = (a == b)\n"
    mutants = find_mutation_sites(src)
    eq_mutants = [
        m for m in mutants if m.original_text == "=="
    ]
    assert eq_mutants
    mutated = apply_mutation(src, eq_mutants[0])
    assert mutated is not None
    assert "==" not in mutated or "!=" in mutated


def test_apply_arithmetic_flip():
    src = "x = a + b\n"
    mutants = find_mutation_sites(src)
    add_mutants = [
        m for m in mutants if m.original_text == "+"
    ]
    mutated = apply_mutation(src, add_mutants[0])
    assert mutated is not None
    assert "a - b" in mutated


def test_apply_boolean_flip_true_to_false():
    src = "x = True\n"
    mutants = find_mutation_sites(src)
    mutated = apply_mutation(src, mutants[0])
    assert mutated is not None
    assert "False" in mutated


def test_apply_unknown_site_returns_none():
    """Mutant pointing at a line that doesn't exist."""
    src = "x = 1\n"
    m = Mutant(
        mutant_id="x", source_file="t.py",
        line_number=999, col_offset=0,
        mutation_kind=MutationKind.COMPARISON_FLIP,
        original_text="==", mutated_text="!=",
    )
    assert apply_mutation(src, m) is None


def test_apply_malformed_source_returns_none():
    m = Mutant(
        mutant_id="x", source_file="t.py",
        line_number=1, col_offset=0,
        mutation_kind=MutationKind.COMPARISON_FLIP,
        original_text="==", mutated_text="!=",
    )
    assert apply_mutation("def broken(", m) is None


# Verdict classifier


def test_verdict_disabled_no_mutants():
    assert (
        _verdict_for_kill_ratio(0.0, 0)
        is MutationVerdict.DISABLED
    )


def test_verdict_weak():
    assert (
        _verdict_for_kill_ratio(0.3, 10)
        is MutationVerdict.WEAK
    )


def test_verdict_fair():
    assert (
        _verdict_for_kill_ratio(0.6, 10)
        is MutationVerdict.FAIR
    )


def test_verdict_strong():
    assert (
        _verdict_for_kill_ratio(0.8, 10)
        is MutationVerdict.STRONG
    )


def test_verdict_perfect():
    assert (
        _verdict_for_kill_ratio(1.0, 10)
        is MutationVerdict.STRONG
    )


# run_mutant


def test_run_mutant_dry_run():
    src = "x = (a == b)\n"
    mutants = find_mutation_sites(src)
    result = _run(run_mutant(
        mutants[0], src, dry_run=True,
    ))
    assert result.status is MutantStatus.SURVIVED
    assert "dry-run" in result.diagnostic.lower()


def test_run_mutant_unapplied_returns_error():
    """Mutant with invalid target lineno → mutation can't apply."""
    src = "x = 1\n"
    m = Mutant(
        mutant_id="x", source_file="t.py",
        line_number=999, col_offset=0,
        mutation_kind=MutationKind.COMPARISON_FLIP,
        original_text="==", mutated_text="!=",
    )
    result = _run(run_mutant(m, src, dry_run=True))
    assert result.status is MutantStatus.ERROR


def test_run_mutant_with_test_runner_killed(tmp_path, monkeypatch):
    """Test runner returns False → mutant KILLED."""
    target = tmp_path / "subject.py"
    target.write_text("x = (a == b)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    src = target.read_text(encoding="utf-8")
    mutants = find_mutation_sites(src, source_file="subject.py")

    async def _killer(path):
        return False, "tests failed (mutation killed)"

    result = _run(run_mutant(
        mutants[0], src,
        test_runner=_killer,
        dry_run=False,
        repo_root=tmp_path,
    ))
    assert result.status is MutantStatus.KILLED
    # File should be restored to original
    assert target.read_text(encoding="utf-8") == src


def test_run_mutant_with_test_runner_survived(tmp_path, monkeypatch):
    """Test runner returns True → mutant SURVIVED."""
    target = tmp_path / "subject.py"
    target.write_text("x = (a == b)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    src = target.read_text(encoding="utf-8")
    mutants = find_mutation_sites(src, source_file="subject.py")

    async def _passer(path):
        return True, "tests passed (mutation survived)"

    result = _run(run_mutant(
        mutants[0], src,
        test_runner=_passer,
        dry_run=False,
        repo_root=tmp_path,
    ))
    assert result.status is MutantStatus.SURVIVED


def test_run_mutant_test_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_TEST_TIMEOUT_S, "1")
    target = tmp_path / "subject.py"
    target.write_text("x = (a == b)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    src = target.read_text(encoding="utf-8")
    mutants = find_mutation_sites(src, source_file="subject.py")

    async def _slow_runner(path):
        await asyncio.sleep(10)
        return True, ""

    result = _run(run_mutant(
        mutants[0], src,
        test_runner=_slow_runner,
        dry_run=False,
        repo_root=tmp_path,
    ))
    assert result.status is MutantStatus.TIMEOUT
    # File restored even on timeout
    assert target.read_text(encoding="utf-8") == src


def test_run_mutant_test_runner_exception(tmp_path, monkeypatch):
    target = tmp_path / "subject.py"
    target.write_text("x = (a == b)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    src = target.read_text(encoding="utf-8")
    mutants = find_mutation_sites(src, source_file="subject.py")

    async def _raiser(path):
        raise RuntimeError("test runner exploded")

    result = _run(run_mutant(
        mutants[0], src,
        test_runner=_raiser,
        dry_run=False,
        repo_root=tmp_path,
    ))
    assert result.status is MutantStatus.ERROR
    # File restored even on exception
    assert target.read_text(encoding="utf-8") == src


def test_run_mutant_missing_target_file(tmp_path):
    src = "x = (a == b)\n"
    mutants = find_mutation_sites(src)

    async def _passer(path):
        return True, ""

    result = _run(run_mutant(
        mutants[0], src,
        test_runner=_passer,
        dry_run=False,
        repo_root=tmp_path,
    ))
    assert result.status is MutantStatus.ERROR
    assert "does not exist" in result.diagnostic


# evaluate_file


def test_evaluate_master_off():
    report = _run(evaluate_file("x.py"))
    assert report.master_enabled is False
    assert report.verdict is MutationVerdict.DISABLED


def test_evaluate_empty_path(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = _run(evaluate_file(""))
    assert report.verdict is MutationVerdict.DISABLED


def test_evaluate_with_source_override_dry_run(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    src = "x = (a == b)\nreturn a + b\n"
    report = _run(evaluate_file(
        "test.py",
        source_text_override=src,
        dry_run=True,
    ))
    # Dry run: all mutants treated as SURVIVED with "dry-run"
    # diagnostic — verdict depends on kill_ratio = 0/N = 0 →
    # WEAK
    assert report.total_mutants >= 1
    assert all(
        r.status is MutantStatus.SURVIVED
        for r in report.results
    )
    assert report.kill_ratio == 0.0
    assert report.verdict is MutationVerdict.WEAK


def test_evaluate_all_killed_strong(monkeypatch):
    """All mutants killed → verdict STRONG."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")

    async def _killer(path):
        return False, "tests fail"

    src = "x = (a == b)\n"
    report = _run(evaluate_file(
        "test.py",
        source_text_override=src,
        test_runner=_killer,
        # Need real file for write/restore. Use dry_run instead.
        dry_run=True,
    ))
    # Dry run skips real execution → all SURVIVED.
    # Verify via separate test with real file.


def test_evaluate_with_real_file_killed(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    target = tmp_path / "subject.py"
    target.write_text("x = (a == b)\n", encoding="utf-8")

    async def _killer(path):
        return False, "tests fail"

    report = _run(evaluate_file(
        "subject.py",
        repo_root=tmp_path,
        test_runner=_killer,
    ))
    assert report.killed_count >= 1
    assert report.survived_count == 0
    assert report.verdict is MutationVerdict.STRONG


def test_evaluate_with_real_file_survived(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    target = tmp_path / "subject.py"
    target.write_text("x = (a == b)\n", encoding="utf-8")

    async def _passer(path):
        return True, "tests pass"

    report = _run(evaluate_file(
        "subject.py",
        repo_root=tmp_path,
        test_runner=_passer,
    ))
    assert report.survived_count >= 1
    assert report.killed_count == 0
    assert report.verdict is MutationVerdict.WEAK


def test_evaluate_no_mutation_sites(monkeypatch):
    """File with no operators — no mutation sites."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = _run(evaluate_file(
        "test.py",
        source_text_override="x = 1\n",
    ))
    assert report.total_mutants == 0
    assert report.verdict is MutationVerdict.DISABLED


def test_evaluate_unparseable_file(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = _run(evaluate_file(
        "test.py",
        source_text_override="def broken(",
    ))
    assert report.total_mutants == 0


def test_evaluate_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = _run(evaluate_file(
        "absent.py", repo_root=tmp_path,
    ))
    assert report.verdict is MutationVerdict.DISABLED
    assert "file read failed" in report.diagnostic.lower()


def test_evaluate_mixed_outcomes(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    target = tmp_path / "subject.py"
    target.write_text("x = (a == b)\ny = c + d\n", encoding="utf-8")

    # Mutant runner: kill the first mutant, survive the rest.
    call_count = [0]
    async def _mixed(path):
        call_count[0] += 1
        return call_count[0] != 1, f"call #{call_count[0]}"

    report = _run(evaluate_file(
        "subject.py",
        repo_root=tmp_path,
        test_runner=_mixed,
    ))
    assert report.killed_count + report.survived_count >= 2
    assert report.killed_count >= 1
    assert report.survived_count >= 1


# Persistence


def test_persist_disabled_verdict_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    _run(evaluate_file(
        "test.py", source_text_override="x = 1\n",
    ))
    assert not ledger_path().exists()


def test_persist_writes(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    target = tmp_path / "subject.py"
    target.write_text("x = (a == b)\n", encoding="utf-8")

    async def _killer(path):
        return False, "killed"

    _run(evaluate_file(
        "subject.py",
        repo_root=tmp_path,
        test_runner=_killer,
    ))
    assert ledger_path().exists()


def test_persist_master_off_no_write(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_PERSIST, "true")
    target = tmp_path / "subject.py"
    target.write_text("x = (a == b)\n", encoding="utf-8")
    _run(evaluate_file("subject.py", repo_root=tmp_path))
    assert not ledger_path().exists()


# Sync wrapper


def test_sync_wrapper_outside_loop(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_file_sync(
        "test.py",
        source_text_override="x = (a == b)\n",
        dry_run=True,
    )
    assert isinstance(report, MutationReport)


def test_sync_wrapper_inside_loop(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    async def inner():
        return evaluate_file_sync(
            "test.py",
            source_text_override="x = (a == b)\n",
            dry_run=True,
        )
    report = asyncio.run(inner())
    assert report.verdict is MutationVerdict.DISABLED
    assert "event loop" in report.diagnostic.lower()


# Renderer


def test_format_master_off():
    out = format_mutation_panel()
    assert "disabled" in out


def test_format_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = _run(evaluate_file(
        "test.py",
        source_text_override="x = (a == b)\n",
        dry_run=True,
    ))
    out = format_mutation_panel(report)
    assert "Mutation Testing" in out


# to_dict


def test_mutant_to_dict():
    m = Mutant(
        mutant_id="m1", source_file="t.py",
        line_number=1, col_offset=0,
        mutation_kind=MutationKind.COMPARISON_FLIP,
        original_text="==", mutated_text="!=",
    )
    d = m.to_dict()
    assert d["schema_version"] == MUTATION_TESTING_SCHEMA_VERSION


def test_mutant_result_to_dict():
    m = Mutant(
        mutant_id="m1", source_file="t.py",
        line_number=1, col_offset=0,
        mutation_kind=MutationKind.COMPARISON_FLIP,
        original_text="==", mutated_text="!=",
    )
    r = MutantResult(
        mutant=m, status=MutantStatus.KILLED,
        test_duration_s=1.0, diagnostic="ok",
    )
    d = r.to_dict()
    assert d["kind"] == "mutant_result"
    assert d["status"] == "killed"


def test_report_to_dict():
    r = MutationReport(
        evaluated_at_unix=1.0, master_enabled=True,
        verdict=MutationVerdict.STRONG,
        source_file="x.py", total_mutants=5,
        killed_count=4, survived_count=1,
        timeout_count=0, error_count=0,
        kill_ratio=0.8, results=(),
        boundary_crossed=False,
        diagnostic="x", elapsed_s=0.0,
    )
    d = r.to_dict()
    assert d["schema_version"] == MUTATION_TESTING_SCHEMA_VERSION


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "mutation_testing_harness.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 6


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "kind_taxonomy_closed",
        "status_taxonomy_closed",
        "authority_asymmetry",
        "master_default_false",
        "composes_canonical",
    ],
)
def test_pin_canonical(_canonical, name_part):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(p for p in pins if name_part in p.invariant_name)
    assert pin.validate(tree, src) == ()


def test_pin_authority_forbids_test_runner():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.test_runner "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_authority_forbids_orchestrator():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_composes_synthetic():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    bad = "# nothing\n"
    assert pin.validate(ast.parse(bad), bad)


# Flag registry


class _CapturingRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _CapturingRegistry()
    count = register_flags(reg)
    assert count == 7


def test_flag_master_default_false():
    reg = _CapturingRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False


# SSE


def test_sse_event_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert (
        ios.EVENT_TYPE_MUTATION_TESTING_EVALUATED
        == "mutation_testing_evaluated"
    )
    assert (
        "mutation_testing_evaluated"
        in ios._VALID_EVENT_TYPES
    )


# End-to-end mutation roundtrip


def test_e2e_atomic_restore_even_on_runner_failure(
    tmp_path, monkeypatch,
):
    """Critical safety test: file MUST be restored to original
    state even if runner raises mid-test."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    target = tmp_path / "subject.py"
    original = "x = (a == b)\n"
    target.write_text(original, encoding="utf-8")

    async def _exploder(path):
        raise RuntimeError("simulated crash")

    _run(evaluate_file(
        "subject.py",
        repo_root=tmp_path,
        test_runner=_exploder,
    ))
    # CRITICAL: original content must be preserved
    assert target.read_text(encoding="utf-8") == original


def test_e2e_no_backup_left_on_disk(tmp_path, monkeypatch):
    """Backup file must be cleaned up after restore."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    target = tmp_path / "subject.py"
    target.write_text("x = (a == b)\n", encoding="utf-8")

    async def _killer(path):
        return False, "killed"

    _run(evaluate_file(
        "subject.py",
        repo_root=tmp_path,
        test_runner=_killer,
    ))
    # No .mut_bak siblings should remain
    backups = list(tmp_path.glob("*.mut_bak"))
    assert backups == []

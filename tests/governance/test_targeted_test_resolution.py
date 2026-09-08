"""A resolved shard must never become the whole repository.

## The measurement

``Strategy 4 (repo fallback)`` appended the repo-level ``tests/`` DIRECTORY
when strategies 0-3 found no test related to a changed file. Pytest does the
obvious thing with a directory.

    pytest --collect-only tests/                 200.9s, 69,353 tests, 32 errors
    pytest --collect-only <one explicit file>      0.67s

Run twice per candidate by the flake retry, that is the ~450s/candidate that
consumed every op's budget in bt-2026-09-08-213932. The log's "Resolved 1 test
targets" was counting one DIRECTORY, which is why it reached every downstream
consumer looking like a one-file shard.

## It was wrong, not just slow

``backend/api/audio_error_fallback.py`` was failed on
``test_generic_batch_round_trip_parses_cleanly`` — which lives in
``tests/adversarial/test_synthetic_adversary.py`` and has no relationship to
the change. A verdict drawn from 69,353 unrelated tests is not a verdict about
the candidate.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import test_runner as TR
from backend.core.ouroboros.governance.test_runner import (
    PythonAdapter,
    TestRunner,
    _bound_targets,
)

REPO = Path(__file__).resolve().parents[2]
#: A source file this repo genuinely has no test for.
UNCOVERED = REPO / "backend/api/audio_error_fallback.py"
#: A source file with a name-convention test.
COVERED = REPO / "backend/core/ouroboros/governance/observability/recorder_lease.py"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(TR._ENV_SUITE_FALLBACK, raising=False)
    monkeypatch.delenv(TR._ENV_BLEED_MAX_FILES, raising=False)
    yield


# --------------------------------------------------------------------------
# The bleed guard — a directory may never reach pytest
# --------------------------------------------------------------------------

def test_a_directory_target_is_refused():
    """THE regression. One directory entry IS the whole suite."""
    admitted, bleed = _bound_targets((REPO / "tests",))
    assert admitted == ()
    assert bleed is not None and "directory" in bleed


def test_a_file_target_passes_through_untouched():
    target = REPO / "tests/governance/test_recorder_lease.py"
    admitted, bleed = _bound_targets((target,))
    assert admitted == (target,)
    assert bleed is None


def test_a_mixed_set_is_NARROWED_not_aborted():
    """Fail closed in the direction that preserves work: a partially-good
    resolution still validates the files it legitimately found."""
    good = REPO / "tests/governance/test_recorder_lease.py"
    admitted, bleed = _bound_targets((good, REPO / "tests"))
    assert admitted == (good,)
    assert bleed is not None


def test_the_set_is_bounded_by_count(monkeypatch, tmp_path):
    monkeypatch.setenv(TR._ENV_BLEED_MAX_FILES, "3")
    files = []
    for i in range(10):
        p = tmp_path / f"test_x{i}.py"
        p.write_text("def test_ok():\n    assert True\n")
        files.append(p)
    admitted, bleed = _bound_targets(tuple(files))
    assert len(admitted) == 3
    assert bleed is not None and "exceeded" in bleed


def test_counting_targets_could_never_have_caught_this():
    """A directory arrives as ONE innocuous entry. Only inspecting the SHAPE
    of each target can distinguish it from a one-file shard."""
    admitted, _ = _bound_targets((REPO / "tests",))
    assert len(admitted) == 0, "a count-based guard would have seen len == 1"


@pytest.mark.parametrize("hostile", [None, (), (123,), ("x",), (object(),)])
def test_the_guard_never_raises(hostile):
    admitted, _bleed = _bound_targets(hostile)
    assert isinstance(admitted, tuple)


def test_the_adapter_bounds_before_it_derives_a_timeout():
    """Order matters: deriving a shard timeout from an unbounded set produces
    a plan for work that must not happen."""
    src = inspect.getsource(PythonAdapter.run)
    assert src.index("_bound_targets(") < src.index("derive_test_timeouts(")


def test_the_bleed_fault_is_named():
    src = inspect.getsource(PythonAdapter.run)
    assert "CollectionBleedFault" in src


# --------------------------------------------------------------------------
# Strategy 4 — no covering test is a FACT, not a gap to fill
# --------------------------------------------------------------------------

def test_an_uncovered_file_resolves_to_NOTHING():
    runner = TestRunner(repo_root=REPO)
    got = asyncio.run(runner.resolve_affected_tests((UNCOVERED,)))
    assert all(not Path(p).is_dir() for p in got), f"a directory survived: {got}"
    assert got == (), f"expected no targets, got {got}"


def test_a_covered_file_still_resolves_to_its_test():
    """The bound must not cost us real resolutions."""
    runner = TestRunner(repo_root=REPO)
    got = asyncio.run(runner.resolve_affected_tests((COVERED,)))
    assert got, "a covered file resolved to nothing — resolution is over-bounded"
    assert any("test_recorder_lease" in str(p) for p in got), got


def test_the_uncovered_case_is_classified_not_passed():
    """Running nothing and calling it a pass is the vacuous validation this
    pipeline has been bitten by before."""
    adapter = PythonAdapter(repo_root=REPO)
    result = asyncio.run(adapter.run((), None, 900.0, "op-test"))
    assert result.passed is False
    assert result.failure_class == "no_covering_test"


def test_the_uncovered_case_is_instant():
    import time as _t

    adapter = PythonAdapter(repo_root=REPO)
    t0 = _t.monotonic()
    asyncio.run(adapter.run((), None, 900.0, "op-test"))
    assert _t.monotonic() - t0 < 5.0


def test_the_legacy_suite_fallback_is_off_by_default():
    assert TR._suite_fallback_enabled() is False


def test_the_operator_can_still_ask_for_a_full_suite_run(monkeypatch):
    """A deliberate full-suite regression pass is legitimate; it just may not
    be what an unresolved single-file change silently becomes."""
    monkeypatch.setenv(TR._ENV_SUITE_FALLBACK, "true")
    assert TR._suite_fallback_enabled() is True
    runner = TestRunner(repo_root=REPO)
    got = asyncio.run(runner.resolve_affected_tests((UNCOVERED,)))
    assert any(Path(p).is_dir() for p in got), "the escape hatch is welded shut"


def test_the_pipeline_already_knew_the_file_was_uncovered():
    """TestCoverageEnforcer logged exactly this for exactly this file in the
    same op, and injected a test-generation instruction. Two components asking
    one question and answering it oppositely was the real defect."""
    from backend.core.ouroboros.governance import intelligence_hooks

    src = inspect.getsource(intelligence_hooks)
    assert "lack test coverage" in src


# --------------------------------------------------------------------------
# The end-to-end number
# --------------------------------------------------------------------------

@pytest.mark.timeout(120)
def test_validating_a_covered_file_is_seconds_not_minutes():
    import time as _t

    runner = TestRunner(repo_root=REPO)
    targets = asyncio.run(runner.resolve_affected_tests((COVERED,)))
    adapter = PythonAdapter(repo_root=REPO)
    t0 = _t.monotonic()
    result = asyncio.run(adapter.run(targets, None, 900.0, "op-measure"))
    elapsed = _t.monotonic() - t0
    assert elapsed < 60.0, f"validation took {elapsed:.1f}s — the suite is back"
    assert result.test_result.total > 0, "validated zero tests — vacuous"

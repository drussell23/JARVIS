"""Slice 66 — swe_bench_pro advisory test-gate in VALIDATE.

Culminating soak (bt-2026-06-02-193445): DW generated a candidate cheaply, but
the op's VALIDATE failed `best_fc='test'` — the candidate's tests ran in the bare
LOCAL env (no qutebrowser/PyQt) → fail → L2 exhausted → op terminal=fail →
eval=unresolved → scorer's RESOLVED-guard skipped → the Slice 65 container engine
was NEVER invoked (0 log lines).

Fix (benchmark-VALID, gated): for swe_bench_pro ops, a `test` failure-class in
VALIDATE is ADVISORY — the local repo env can't run the tests AND running the
held-out fail_to_pass tests in VALIDATE would LEAK them into the L2 repair loop
(gaming the score). So a test-only failure is promoted to passed → the candidate
reaches APPLY (captured) → the ONE-SHOT container scoring (Slice 65, autoscore
layer) is the authoritative held-out judge. Syntax/build/infra failures STILL
block (valid local checks). Non-swe_bench ops + non-test failures are byte-
identical.

This is NOT a bypass: the local test-run is meaningless without the env, and the
authoritative test (the held-out container scoring) is preserved exactly once.
"""
from __future__ import annotations

from backend.core.ouroboros.governance.orchestrator import _swe_bench_test_advisory
from backend.core.ouroboros.governance.op_context import ValidationResult


def _vr(passed: bool, fc, cand=None) -> ValidationResult:
    return ValidationResult(
        passed=passed, best_candidate=cand, validation_duration_s=0.1,
        error=None if passed else "boom", failure_class=fc,
    )


def test_swe_bench_test_failure_promoted_to_passed():
    cand = {"full_content": "patch", "file_path": "qutebrowser/misc/guiprocess.py"}
    r = _swe_bench_test_advisory("swe_bench_pro", "op1", cand, _vr(False, "test"))
    assert r.passed is True
    assert r.best_candidate is cand          # candidate carried forward to APPLY
    assert r.failure_class is None           # promoted (advisory)
    assert r.error is None


def test_swe_bench_build_failure_still_blocks():
    # Valid LOCAL check — a malformed patch must still fail VALIDATE.
    r = _swe_bench_test_advisory("swe_bench_pro", "op1", {}, _vr(False, "build"))
    assert r.passed is False and r.failure_class == "build"


def test_swe_bench_infra_failure_still_blocks():
    r = _swe_bench_test_advisory("swe_bench_pro", "op1", {}, _vr(False, "infra"))
    assert r.passed is False and r.failure_class == "infra"


def test_non_swe_bench_test_failure_unchanged():
    # Organic self-dev ops keep the strict local test-gate (byte-identical).
    r = _swe_bench_test_advisory("opportunity_miner", "op1", {}, _vr(False, "test"))
    assert r.passed is False and r.failure_class == "test"


def test_already_passed_unchanged():
    r = _swe_bench_test_advisory("swe_bench_pro", "op1", {}, _vr(True, None))
    assert r.passed is True


def test_empty_source_unchanged():
    r = _swe_bench_test_advisory("", "op1", {}, _vr(False, "test"))
    assert r.passed is False and r.failure_class == "test"

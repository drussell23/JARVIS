"""Phase 2 Slice 2.2 — RepeatRunner regression spine.

Pins:
  §1   repeat_runner_enabled flag — default false; case-tolerant
  §2   RunBudget — sentinels resolve to env defaults
  §3   RunBudget — explicit values override defaults
  §4   RunBudget — clamps invalid values gracefully
  §5   RepeatVerdict — frozen + .passed + .is_terminal helpers
  §6   RepeatVerdict — total_decisive_runs accessor
  §7   Single all-pass batch → PASSED with high confidence
  §8   Single all-fail batch → FAILED with high confidence
  §9   Mixed pass/fail → INSUFFICIENT_EVIDENCE if neither hits threshold
  §10  Early-stop on converged_pass triggers before max_runs
  §11  Early-stop on converged_fail triggers before max_runs
  §12  early_stop=False runs full max_runs even when converged
  §13  min_runs gate prevents premature early-stop
  §14  Non-Mapping evidence → EVALUATOR_ERROR (run-level defensive)
  §15  Collector raises → EVALUATOR_ERROR (no propagation)
  §16  None Property → graceful EVALUATOR_ERROR top-level
  §17  Insufficient/error verdicts don't update belief
  §18  Parallel batch concurrency (env-tunable)
  §19  Bayesian update integration with Antigravity exploration_calculus
  §20  Fallback Bayesian update when calculus unavailable
  §21  RepeatVerdict.individual_verdicts captures all runs
  §22  Authority invariants — no orchestrator/phase_runner/provider imports
  §23  Verdict→calculus-string mapping (PASSED/FAILED/INSUFFICIENT)
  §24  Public API exposed from package __init__
  §25  Singleton accessor returns same instance
  §26  Posterior clamped to (0, 1) — no degenerate beliefs
"""
from __future__ import annotations

import asyncio
from typing import Any, Mapping
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.verification import (
    Property,
    PropertyVerdict,
    RepeatRunner,
    RepeatVerdict,
    RunBudget,
    VerdictKind,
    get_default_oracle,
    get_default_runner,
    register_evaluator,
    repeat_runner_enabled,
)
from backend.core.ouroboros.governance.verification.property_oracle import (
    reset_registry_for_tests,
)
from backend.core.ouroboros.governance.verification.repeat_runner import (
    REPEAT_VERDICT_SCHEMA_VERSION,
    _bayesian_update_safely,
    _fallback_bayesian_update,
    _verdict_to_calculus_str,
)


@pytest.fixture
def fresh_registry():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# ---------------------------------------------------------------------------
# §1 — Master flag
# ---------------------------------------------------------------------------


def test_runner_default_false(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_VERIFICATION_REPEAT_RUNNER_ENABLED", raising=False,
    )
    assert repeat_runner_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_runner_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_REPEAT_RUNNER_ENABLED", val,
    )
    assert repeat_runner_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage", ""])
def test_runner_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_REPEAT_RUNNER_ENABLED", val,
    )
    assert repeat_runner_enabled() is False


# ---------------------------------------------------------------------------
# §2-§4 — RunBudget
# ---------------------------------------------------------------------------


def test_run_budget_sentinels_resolve_to_env_defaults() -> None:
    b = RunBudget()
    assert b.resolved_min_runs() == 5
    assert b.resolved_max_runs() == 50
    assert abs(b.resolved_confidence() - 0.95) < 1e-6
    assert b.resolved_concurrency() == 4
    assert abs(b.resolved_prior() - 0.5) < 1e-6


def test_run_budget_explicit_overrides() -> None:
    b = RunBudget(
        min_runs=2, max_runs=10, confidence_threshold=0.99,
        early_stop=False, parallel_concurrency=8, initial_prior=0.7,
    )
    assert b.resolved_min_runs() == 2
    assert b.resolved_max_runs() == 10
    assert abs(b.resolved_confidence() - 0.99) < 1e-6
    assert b.early_stop is False
    assert b.resolved_concurrency() == 8
    assert abs(b.resolved_prior() - 0.7) < 1e-6


def test_run_budget_clamps_invalid_confidence() -> None:
    """Confidence ≤ 0.5 falls through to env default (meaningless)."""
    b = RunBudget(confidence_threshold=0.3)
    # Falls through to env default 0.95
    assert abs(b.resolved_confidence() - 0.95) < 1e-6


def test_run_budget_clamps_invalid_prior() -> None:
    """Prior is clamped to (0.001, 0.999) per Antigravity convention."""
    assert abs(RunBudget(initial_prior=0.0).resolved_prior() - 0.001) < 1e-3
    assert abs(RunBudget(initial_prior=1.0).resolved_prior() - 0.999) < 1e-3
    assert abs(
        RunBudget(initial_prior=-5.0).resolved_prior() - 0.001
    ) < 1e-3


def test_run_budget_env_override(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_VERIFICATION_REPEAT_MIN_RUNS", "3")
    monkeypatch.setenv("JARVIS_VERIFICATION_REPEAT_MAX_RUNS", "20")
    monkeypatch.setenv("JARVIS_VERIFICATION_REPEAT_CONFIDENCE", "0.90")
    monkeypatch.setenv("JARVIS_VERIFICATION_REPEAT_CONCURRENCY", "2")
    b = RunBudget()
    assert b.resolved_min_runs() == 3
    assert b.resolved_max_runs() == 20
    assert abs(b.resolved_confidence() - 0.90) < 1e-6
    assert b.resolved_concurrency() == 2


# ---------------------------------------------------------------------------
# §5-§6 — RepeatVerdict
# ---------------------------------------------------------------------------


def test_repeat_verdict_is_frozen() -> None:
    v = RepeatVerdict(
        property_name="x", kind="y", runs_completed=5,
        pass_count=4, fail_count=1, insufficient_count=0, error_count=0,
        initial_prior=0.5, posterior=0.95, confidence=0.95,
        final_verdict=VerdictKind.PASSED, early_stopped=True,
        halted_reason="converged_pass",
    )
    with pytest.raises(Exception):
        v.runs_completed = 10  # type: ignore[misc]


def test_repeat_verdict_passed_helper() -> None:
    v = RepeatVerdict(
        property_name="x", kind="y", runs_completed=5,
        pass_count=5, fail_count=0, insufficient_count=0, error_count=0,
        initial_prior=0.5, posterior=0.99, confidence=0.99,
        final_verdict=VerdictKind.PASSED, early_stopped=True,
        halted_reason="converged_pass",
    )
    assert v.passed is True
    assert v.is_terminal is True


def test_repeat_verdict_total_decisive_runs() -> None:
    v = RepeatVerdict(
        property_name="x", kind="y", runs_completed=10,
        pass_count=5, fail_count=2, insufficient_count=2, error_count=1,
        initial_prior=0.5, posterior=0.7, confidence=0.7,
        final_verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
        early_stopped=False, halted_reason="max_runs_reached",
    )
    assert v.total_decisive_runs == 7  # 5 + 2 (excludes insuff/err)


def test_repeat_verdict_schema_version_pinned() -> None:
    assert REPEAT_VERDICT_SCHEMA_VERSION == "repeat_verdict.1"


# ---------------------------------------------------------------------------
# §7-§9 — All-pass / all-fail / mixed runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_pass_yields_passed(fresh_registry) -> None:
    """10 runs all PASS → posterior > 0.95 → final PASSED."""
    runner = RepeatRunner()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )

    async def collector(idx: int) -> Mapping[str, Any]:
        return {"exit_code": 0}

    result = await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(min_runs=5, max_runs=20),
    )
    assert result.final_verdict is VerdictKind.PASSED
    assert result.pass_count >= 5  # at least min_runs
    assert result.fail_count == 0
    assert result.posterior > 0.95
    assert result.passed is True


@pytest.mark.asyncio
async def test_all_fail_yields_failed(fresh_registry) -> None:
    runner = RepeatRunner()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )

    async def collector(idx: int) -> Mapping[str, Any]:
        return {"exit_code": 1}

    result = await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(min_runs=5, max_runs=20),
    )
    assert result.final_verdict is VerdictKind.FAILED
    assert result.fail_count >= 5
    assert result.pass_count == 0
    assert result.posterior < 0.05
    assert (1.0 - result.posterior) > 0.95


@pytest.mark.asyncio
async def test_balanced_mix_insufficient_evidence(
    fresh_registry,
) -> None:
    """Alternating pass/fail → posterior stays near prior → INSUFFICIENT."""
    runner = RepeatRunner()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )

    async def collector(idx: int) -> Mapping[str, Any]:
        return {"exit_code": 0 if idx % 2 == 0 else 1}

    result = await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(min_runs=10, max_runs=10, early_stop=False),
    )
    # Alternating pass/fail → ~50/50 → no high-confidence verdict
    assert result.final_verdict is VerdictKind.INSUFFICIENT_EVIDENCE
    assert result.pass_count == 5
    assert result.fail_count == 5


# ---------------------------------------------------------------------------
# §10-§13 — Early-stop semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_early_stop_on_converged_pass(fresh_registry) -> None:
    """All-pass triggers early-stop after min_runs."""
    runner = RepeatRunner()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )
    call_count = {"n": 0}

    async def collector(idx: int) -> Mapping[str, Any]:
        call_count["n"] += 1
        return {"exit_code": 0}

    result = await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(
            min_runs=5, max_runs=100,
            confidence_threshold=0.95, early_stop=True,
            parallel_concurrency=1,  # sequential for clean accounting
        ),
    )
    assert result.early_stopped is True
    assert result.halted_reason == "converged_pass"
    assert result.runs_completed < 100
    # All-pass → 5 runs of LR=3.0 starting from prior=0.5:
    # posterior ~= 0.996, well above 0.95 — converges by run 5
    assert result.runs_completed >= 5


@pytest.mark.asyncio
async def test_early_stop_on_converged_fail(fresh_registry) -> None:
    runner = RepeatRunner()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )

    async def collector(idx: int) -> Mapping[str, Any]:
        return {"exit_code": 1}

    result = await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(
            min_runs=5, max_runs=100,
            confidence_threshold=0.95, early_stop=True,
            parallel_concurrency=1,
        ),
    )
    assert result.early_stopped is True
    assert result.halted_reason == "converged_fail"
    assert result.runs_completed >= 5
    assert result.runs_completed < 100


@pytest.mark.asyncio
async def test_early_stop_disabled_runs_full_budget(
    fresh_registry,
) -> None:
    runner = RepeatRunner()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )

    async def collector(idx: int) -> Mapping[str, Any]:
        return {"exit_code": 0}

    result = await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(
            min_runs=5, max_runs=15, early_stop=False,
            parallel_concurrency=1,
        ),
    )
    assert result.early_stopped is False
    assert result.runs_completed == 15


@pytest.mark.asyncio
async def test_min_runs_floors_early_stop(fresh_registry) -> None:
    """Even with all-pass + tight confidence, can't early-stop
    before min_runs."""
    runner = RepeatRunner()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )

    async def collector(idx: int) -> Mapping[str, Any]:
        return {"exit_code": 0}

    # min_runs=10 forces at least 10 runs even though convergence
    # would happen by run 5
    result = await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(
            min_runs=10, max_runs=20, early_stop=True,
            parallel_concurrency=1,
        ),
    )
    assert result.runs_completed >= 10


# ---------------------------------------------------------------------------
# §14-§16 — Defensive paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_mapping_evidence_becomes_error(fresh_registry) -> None:
    """Collector returning non-Mapping → EVALUATOR_ERROR per run."""
    runner = RepeatRunner()
    p = Property.make(kind="test_passes", name="t")

    async def collector(idx: int) -> Mapping[str, Any]:
        return "not a mapping"  # type: ignore[return-value]

    result = await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(min_runs=3, max_runs=3, early_stop=False),
    )
    assert result.error_count == 3
    # Errors don't update belief — posterior stays at prior
    assert abs(result.posterior - 0.5) < 1e-6
    # Insuff/error dominated → INSUFFICIENT_EVIDENCE
    assert result.final_verdict is VerdictKind.INSUFFICIENT_EVIDENCE


@pytest.mark.asyncio
async def test_collector_raises_becomes_error(fresh_registry) -> None:
    """Collector exception → EVALUATOR_ERROR with traceback."""
    runner = RepeatRunner()
    p = Property.make(kind="test_passes", name="t")

    async def collector(idx: int) -> Mapping[str, Any]:
        raise RuntimeError(f"simulated fault on run {idx}")

    result = await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(min_runs=3, max_runs=3, early_stop=False),
    )
    assert result.error_count == 3
    # Reasons should mention the runtime error
    assert any(
        "simulated fault" in v.reason
        for v in result.individual_verdicts
    )


@pytest.mark.asyncio
async def test_none_property_graceful() -> None:
    runner = RepeatRunner()

    async def collector(idx: int) -> Mapping[str, Any]:
        return {}

    result = await runner.run(
        prop=None, evidence_collector=collector,  # type: ignore[arg-type]
    )
    assert result.final_verdict is VerdictKind.EVALUATOR_ERROR
    assert result.halted_reason == "property_is_none"


# ---------------------------------------------------------------------------
# §17 — Insufficient/error verdicts don't update belief
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insufficient_verdicts_dont_move_belief(
    fresh_registry,
) -> None:
    """If all runs return INSUFFICIENT_EVIDENCE (e.g., missing
    evidence keys), posterior stays at prior."""
    runner = RepeatRunner()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )

    async def collector(idx: int) -> Mapping[str, Any]:
        return {}  # missing exit_code → INSUFFICIENT_EVIDENCE

    result = await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(
            min_runs=5, max_runs=5, early_stop=False,
            initial_prior=0.7,
        ),
    )
    assert result.insufficient_count == 5
    assert result.pass_count == 0
    assert result.fail_count == 0
    # Belief stayed at prior = 0.7 (no LR updates)
    assert abs(result.posterior - 0.7) < 1e-6


# ---------------------------------------------------------------------------
# §18 — Parallel batching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_concurrency_actually_parallel(
    fresh_registry,
) -> None:
    """concurrency=4 with 4 slow collectors should complete in
    ~1× the slow time, not 4×."""
    import time
    runner = RepeatRunner()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )

    async def slow_collector(idx: int) -> Mapping[str, Any]:
        await asyncio.sleep(0.05)  # 50ms simulated work
        return {"exit_code": 0}

    started = time.monotonic()
    await runner.run(
        prop=p, evidence_collector=slow_collector,
        budget=RunBudget(
            min_runs=4, max_runs=4, early_stop=False,
            parallel_concurrency=4,
        ),
    )
    elapsed = time.monotonic() - started
    # 4 runs at 50ms in parallel → ~50ms total, not ~200ms
    # Allow generous slack for scheduling overhead
    assert elapsed < 0.15, f"runs not parallel — elapsed={elapsed:.3f}s"


@pytest.mark.asyncio
async def test_concurrency_eq_one_sequential(fresh_registry) -> None:
    """concurrency=1 forces sequential execution — useful for
    clean ordinal-tracking tests."""
    runner = RepeatRunner()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )
    indices_seen = []

    async def collector(idx: int) -> Mapping[str, Any]:
        indices_seen.append(idx)
        return {"exit_code": 0}

    await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(
            min_runs=5, max_runs=5, early_stop=False,
            parallel_concurrency=1,
        ),
    )
    # Each call gets a unique 0-indexed run number
    assert sorted(indices_seen) == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# §19-§20 — Bayesian update integration + fallback
# ---------------------------------------------------------------------------


def test_verdict_to_calculus_string_mapping() -> None:
    assert _verdict_to_calculus_str(VerdictKind.PASSED) == "CONFIRMED"
    assert _verdict_to_calculus_str(VerdictKind.FAILED) == "REFUTED"
    assert _verdict_to_calculus_str(
        VerdictKind.INSUFFICIENT_EVIDENCE,
    ) == "INCONCLUSIVE"
    assert _verdict_to_calculus_str(
        VerdictKind.EVALUATOR_ERROR,
    ) == "INCONCLUSIVE"


def test_bayesian_update_via_antigravity_calculus() -> None:
    """A PASSED verdict moves posterior up; FAILED moves down."""
    # Prior 0.5, PASSED → posterior > 0.5
    p_after_pass = _bayesian_update_safely(0.5, VerdictKind.PASSED)
    assert p_after_pass > 0.5
    # Prior 0.5, FAILED → posterior < 0.5
    p_after_fail = _bayesian_update_safely(0.5, VerdictKind.FAILED)
    assert p_after_fail < 0.5
    # INSUFFICIENT/ERROR → no change
    assert (
        _bayesian_update_safely(0.5, VerdictKind.INSUFFICIENT_EVIDENCE)
        == 0.5
    )
    assert (
        _bayesian_update_safely(0.5, VerdictKind.EVALUATOR_ERROR)
        == 0.5
    )


def test_fallback_bayesian_update_works_without_calculus() -> None:
    """When exploration_calculus is unavailable, the fallback math
    produces sound results matching the default LR convention."""
    p_after_pass = _fallback_bayesian_update(0.5, VerdictKind.PASSED)
    assert p_after_pass > 0.5
    p_after_fail = _fallback_bayesian_update(0.5, VerdictKind.FAILED)
    assert p_after_fail < 0.5


def test_bayesian_update_falls_back_on_import_failure() -> None:
    """If the adaptation module is patched to fail import,
    _bayesian_update_safely uses the fallback."""
    # Use a ModuleNotFoundError raised at import to simulate
    with patch.dict(
        "sys.modules",
        {"backend.core.ouroboros.governance.adaptation.exploration_calculus": None},
    ):
        # Force re-import path to fail
        result = _bayesian_update_safely(0.5, VerdictKind.PASSED)
        # Must still return a valid posterior
        assert 0.001 <= result <= 0.999


# ---------------------------------------------------------------------------
# §21 — individual_verdicts captured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_individual_verdicts_captured(fresh_registry) -> None:
    runner = RepeatRunner()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )

    async def collector(idx: int) -> Mapping[str, Any]:
        return {"exit_code": 0 if idx < 3 else 1}

    result = await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(
            min_runs=5, max_runs=5, early_stop=False,
            parallel_concurrency=1,
        ),
    )
    assert len(result.individual_verdicts) == 5
    # First 3 PASSED, last 2 FAILED
    pass_idx = [
        i for i, v in enumerate(result.individual_verdicts)
        if v.verdict is VerdictKind.PASSED
    ]
    fail_idx = [
        i for i, v in enumerate(result.individual_verdicts)
        if v.verdict is VerdictKind.FAILED
    ]
    assert pass_idx == [0, 1, 2]
    assert fail_idx == [3, 4]


# ---------------------------------------------------------------------------
# §22 — Authority invariants
# ---------------------------------------------------------------------------


def test_no_orchestrator_imports() -> None:
    import inspect
    from backend.core.ouroboros.governance.verification import (
        repeat_runner,
    )
    src = inspect.getsource(repeat_runner)
    forbidden = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.phase_runner ",
        "from backend.core.ouroboros.governance.candidate_generator",
    )
    for f in forbidden:
        assert f not in src, f"repeat_runner must NOT contain {f!r}"


def test_no_phase_runners_imports() -> None:
    """AST-walk: no actual import statements pull from phase_runners.
    Docstring mentions of 'phase_runners' (e.g., "we do not import
    any phase_runners/* module") are not import sites — use AST."""
    import ast
    import inspect
    from backend.core.ouroboros.governance.verification import (
        repeat_runner,
    )
    tree = ast.parse(inspect.getsource(repeat_runner))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "phase_runners" not in node.module, (
                f"forbidden import: from {node.module} ..."
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "phase_runners" not in alias.name, (
                    f"forbidden import: import {alias.name}"
                )


def test_no_provider_imports() -> None:
    import inspect
    from backend.core.ouroboros.governance.verification import (
        repeat_runner,
    )
    src = inspect.getsource(repeat_runner)
    assert "doubleword_provider" not in src
    assert "claude_provider" not in src.lower()


# ---------------------------------------------------------------------------
# §23 — Public API + singleton
# ---------------------------------------------------------------------------


def test_public_api_via_package_init() -> None:
    from backend.core.ouroboros.governance import verification
    assert "RepeatRunner" in verification.__all__
    assert "RepeatVerdict" in verification.__all__
    assert "RunBudget" in verification.__all__
    assert "EvidenceCollector" in verification.__all__
    assert "get_default_runner" in verification.__all__
    assert "repeat_runner_enabled" in verification.__all__


def test_runner_singleton_returns_same_instance() -> None:
    r1 = get_default_runner()
    r2 = get_default_runner()
    assert r1 is r2


# ---------------------------------------------------------------------------
# §26 — Posterior clamped — no degenerate beliefs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_posterior_never_exact_zero_or_one(fresh_registry) -> None:
    """Even after 50 all-pass runs, posterior is clamped below 1.0
    so further updates remain meaningful."""
    runner = RepeatRunner()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )

    async def collector(idx: int) -> Mapping[str, Any]:
        return {"exit_code": 0}

    result = await runner.run(
        prop=p, evidence_collector=collector,
        budget=RunBudget(
            min_runs=50, max_runs=50, early_stop=False,
            parallel_concurrency=10,
        ),
    )
    assert 0.001 <= result.posterior <= 0.999
    assert result.passed is True

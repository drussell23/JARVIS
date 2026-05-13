"""Regression spine - SWE-Bench-Pro Phase E parallel evaluation rig.

Phase E drives N problems concurrently through the canonical
B.2.2 -> C -> D pipeline using bounded concurrency from the
canonical hot-reload-safe semaphore.

Spine invariants
----------------

  1. Empty iterable -> yields nothing.
  2. Single problem -> yields one record.
  3. Multiple problems -> yields all N records (count match).
  4. concurrency=1 forces serial (only one in-flight at a time).
  5. concurrency=N admits up to N concurrent (max-in-flight counter
     verified via instrumented fake intake).
  6. score_each=True invokes Phase C scorer.
  7. score_each=False produces a synthetic SKIPPED scoring half.
  8. record_each=True persists into the injected store.
  9. record_each=False does not persist.
 10. Per-task contract violation (evaluator raises unexpectedly)
     yields a synthetic record; rig continues with other tasks.
 11. progress_callback fires per completion with a frozen
     ParallelEvalProgress snapshot.
 12. ParallelEvalProgress is a frozen dataclass.

AST pins
--------

 13. Composes _process_singletons.get_semaphore (canonical
     hot-reload-safe primitive).
 14. Composes evaluate_problem + score_evaluation + EvaluationResultStore
     + EvaluationRecord (no parallel implementations).
 15. No `asyncio.Semaphore(` literal in the module body (the canonical
     get_semaphore is the single seam).
 16. No `while True` polling loop in the rig body.

 17. FlagRegistry seed PARALLEL_CONCURRENCY_ENV_VAR registered.
"""
from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
from typing import Any, Iterator, List, Optional

import pytest

from backend.core.ouroboros.governance._process_singletons import (
    reset_for_test,
)
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    MASTER_FLAG_ENV_VAR,
    ProblemSpec,
)
from backend.core.ouroboros.governance.swe_bench_pro.evaluator import (
    EvaluationOutcome,
    EvaluationResult,
)
from backend.core.ouroboros.governance.swe_bench_pro.parallel_eval import (
    PARALLEL_CONCURRENCY_ENV_VAR,
    ParallelEvalProgress,
    parallel_evaluate,
    register_flags,
)
from backend.core.ouroboros.governance.swe_bench_pro.result_store import (
    EvaluationRecord,
    EvaluationResultStore,
    reset_default_store,
)
from backend.core.ouroboros.governance.swe_bench_pro.scorer import (
    ScoreOutcome,
    ScoringResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv(PARALLEL_CONCURRENCY_ENV_VAR, raising=False)
    reset_default_store()
    reset_for_test()  # Drop process-wide semaphores
    yield
    reset_default_store()
    reset_for_test()


def _make_problem(instance_id: str) -> ProblemSpec:
    return ProblemSpec(
        instance_id=instance_id,
        repo="r/r",
        base_commit="abc",
        problem_statement="fix it",
        test_patch="",
        gold_patch="",
        repo_url="",
    )


# ---------------------------------------------------------------------------
# Stubs - replace evaluate_problem / score_evaluation in the rig's
# module namespace so the spine never reaches real git / pytest.
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Replace evaluate_problem + score_evaluation in the rig's
    namespace with fast deterministic stubs that record which
    instance_ids were processed."""
    state = {
        "eval_calls": [],
        "score_calls": [],
        "in_flight": 0,
        "max_in_flight": 0,
        "lock": threading.Lock(),
    }

    async def _fake_eval(problem, **_kwargs):
        with state["lock"]:
            state["in_flight"] += 1
            state["max_in_flight"] = max(
                state["max_in_flight"], state["in_flight"],
            )
        state["eval_calls"].append(problem.instance_id)
        try:
            # Tiny yield so concurrency-vs-serial timing is observable
            # without flake.
            await asyncio.sleep(0.02)
            return EvaluationResult(
                outcome=EvaluationOutcome.RESOLVED,
                problem_instance_id=problem.instance_id,
                op_id=f"op-{problem.instance_id}",
                terminal_state="applied",
                captured_patch="dummy",
            )
        finally:
            with state["lock"]:
                state["in_flight"] -= 1

    async def _fake_score(ev_result, problem, **_kwargs):
        state["score_calls"].append(problem.instance_id)
        return ScoringResult(
            outcome=ScoreOutcome.PASS,
            problem_instance_id=problem.instance_id,
            tests_passed=2,
            tests_total=2,
            pass_rate=1.0,
        )

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro.parallel_eval."
        "evaluate_problem", _fake_eval,
    )
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro.parallel_eval."
        "score_evaluation", _fake_score,
    )
    return state


# ---------------------------------------------------------------------------
# 1. Empty iterable
# ---------------------------------------------------------------------------


def test_empty_iterable_yields_nothing(
    clean_env: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    async def _collect():
        out: List[EvaluationRecord] = []
        async for r in parallel_evaluate([], intake_service=None):
            out.append(r)
        return out

    out = asyncio.run(_collect())
    assert out == []


# ---------------------------------------------------------------------------
# 2. Single problem
# ---------------------------------------------------------------------------


def test_single_problem_yields_one_record(
    clean_env: None, monkeypatch: pytest.MonkeyPatch,
    patch_pipeline: Any,
) -> None:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    async def _collect():
        out: List[EvaluationRecord] = []
        async for r in parallel_evaluate(
            [_make_problem("inst-1")],
            intake_service=object(),
            concurrency=2,
            record_each=False,
        ):
            out.append(r)
        return out

    out = asyncio.run(_collect())
    assert len(out) == 1
    assert out[0].evaluation.problem_instance_id == "inst-1"


# ---------------------------------------------------------------------------
# 3. Multiple problems
# ---------------------------------------------------------------------------


def test_multi_problem_yields_all(
    clean_env: None, monkeypatch: pytest.MonkeyPatch,
    patch_pipeline: Any,
) -> None:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    problems = [_make_problem(f"inst-{i}") for i in range(5)]

    async def _collect():
        out: List[EvaluationRecord] = []
        async for r in parallel_evaluate(
            problems, intake_service=object(),
            concurrency=3, record_each=False,
        ):
            out.append(r)
        return out

    out = asyncio.run(_collect())
    assert len(out) == 5
    yielded_ids = {r.evaluation.problem_instance_id for r in out}
    assert yielded_ids == {f"inst-{i}" for i in range(5)}


# ---------------------------------------------------------------------------
# 4. concurrency=1 forces serial
# ---------------------------------------------------------------------------


def test_concurrency_one_forces_serial(
    clean_env: None, monkeypatch: pytest.MonkeyPatch,
    patch_pipeline: Any,
) -> None:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    problems = [_make_problem(f"inst-{i}") for i in range(5)]

    async def _drain():
        async for _ in parallel_evaluate(
            problems, intake_service=object(),
            concurrency=1, record_each=False,
        ):
            pass

    asyncio.run(_drain())
    assert patch_pipeline["max_in_flight"] == 1


# ---------------------------------------------------------------------------
# 5. concurrency=N admits N concurrent
# ---------------------------------------------------------------------------


def test_concurrency_n_admits_up_to_n_concurrent(
    clean_env: None, monkeypatch: pytest.MonkeyPatch,
    patch_pipeline: Any,
) -> None:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    problems = [_make_problem(f"inst-{i}") for i in range(6)]

    async def _drain():
        async for _ in parallel_evaluate(
            problems, intake_service=object(),
            concurrency=3, record_each=False,
        ):
            pass

    asyncio.run(_drain())
    # Max-in-flight may be up to 3 (semaphore cap). Serial would
    # show 1; unbounded would show 6. Cap=3 should be reached
    # given the per-task asyncio.sleep yield.
    assert patch_pipeline["max_in_flight"] >= 2
    assert patch_pipeline["max_in_flight"] <= 3


# ---------------------------------------------------------------------------
# 6. score_each=True invokes Phase C scorer
# ---------------------------------------------------------------------------


def test_score_each_true_calls_scorer(
    clean_env: None, monkeypatch: pytest.MonkeyPatch,
    patch_pipeline: Any,
) -> None:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    problems = [_make_problem(f"inst-{i}") for i in range(3)]

    async def _drain():
        async for _ in parallel_evaluate(
            problems, intake_service=object(),
            concurrency=3, score_each=True, record_each=False,
        ):
            pass

    asyncio.run(_drain())
    assert sorted(patch_pipeline["score_calls"]) == sorted(
        f"inst-{i}" for i in range(3)
    )


# ---------------------------------------------------------------------------
# 7. score_each=False yields synthetic SKIPPED scoring
# ---------------------------------------------------------------------------


def test_score_each_false_produces_skipped_scoring(
    clean_env: None, monkeypatch: pytest.MonkeyPatch,
    patch_pipeline: Any,
) -> None:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    async def _collect():
        out: List[EvaluationRecord] = []
        async for r in parallel_evaluate(
            [_make_problem("inst-1")],
            intake_service=object(),
            concurrency=1, score_each=False, record_each=False,
        ):
            out.append(r)
        return out

    out = asyncio.run(_collect())
    assert len(out) == 1
    assert out[0].scoring.outcome == ScoreOutcome.SKIPPED
    assert "scoring_disabled" in out[0].scoring.diagnostic
    # Scorer was NOT invoked.
    assert patch_pipeline["score_calls"] == []


# ---------------------------------------------------------------------------
# 8. record_each=True persists to injected store
# ---------------------------------------------------------------------------


def test_record_each_true_persists_to_store(
    clean_env: None, monkeypatch: pytest.MonkeyPatch,
    patch_pipeline: Any,
) -> None:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    store = EvaluationResultStore(persistence_enabled=False)
    problems = [_make_problem(f"inst-{i}") for i in range(4)]

    async def _drain():
        async for _ in parallel_evaluate(
            problems, intake_service=object(),
            concurrency=2, record_each=True, store=store,
        ):
            pass

    asyncio.run(_drain())
    assert len(store) == 4


# ---------------------------------------------------------------------------
# 9. record_each=False does not persist
# ---------------------------------------------------------------------------


def test_record_each_false_skips_persistence(
    clean_env: None, monkeypatch: pytest.MonkeyPatch,
    patch_pipeline: Any,
) -> None:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    store = EvaluationResultStore(persistence_enabled=False)
    problems = [_make_problem(f"inst-{i}") for i in range(3)]

    async def _drain():
        async for _ in parallel_evaluate(
            problems, intake_service=object(),
            concurrency=2, record_each=False, store=store,
        ):
            pass

    asyncio.run(_drain())
    assert len(store) == 0


# ---------------------------------------------------------------------------
# 10. Per-task contract violation -> synthetic record
# ---------------------------------------------------------------------------


def test_per_task_exception_yields_synthetic_record(
    clean_env: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    async def _raise_for_two(problem, **_kwargs):
        if problem.instance_id == "inst-2":
            raise RuntimeError("synthetic_eval_failure")
        return EvaluationResult(
            outcome=EvaluationOutcome.RESOLVED,
            problem_instance_id=problem.instance_id,
            op_id=f"op-{problem.instance_id}",
            terminal_state="applied",
            captured_patch="dummy",
        )

    async def _fake_score(ev_result, problem, **_kwargs):
        return ScoringResult(
            outcome=ScoreOutcome.PASS,
            problem_instance_id=problem.instance_id,
            tests_passed=1, tests_total=1, pass_rate=1.0,
        )

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro.parallel_eval."
        "evaluate_problem", _raise_for_two,
    )
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro.parallel_eval."
        "score_evaluation", _fake_score,
    )

    problems = [_make_problem(f"inst-{i}") for i in range(4)]

    async def _collect():
        out: List[EvaluationRecord] = []
        async for r in parallel_evaluate(
            problems, intake_service=object(),
            concurrency=2, record_each=False,
        ):
            out.append(r)
        return out

    out = asyncio.run(_collect())
    # All 4 records yielded (synthetic for inst-2).
    assert len(out) == 4
    synthetic = next(
        r for r in out
        if r.evaluation.problem_instance_id == "inst-2"
    )
    assert synthetic.evaluation.outcome == EvaluationOutcome.INGEST_FAILED
    assert synthetic.scoring.outcome == ScoreOutcome.SCORING_ERROR
    assert "evaluator_raised:RuntimeError" in synthetic.scoring.diagnostic


# ---------------------------------------------------------------------------
# 11. progress_callback fires per completion
# ---------------------------------------------------------------------------


def test_progress_callback_fires_per_completion(
    clean_env: None, monkeypatch: pytest.MonkeyPatch,
    patch_pipeline: Any,
) -> None:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    snapshots: List[ParallelEvalProgress] = []

    def _cb(snapshot: ParallelEvalProgress) -> None:
        snapshots.append(snapshot)

    problems = [_make_problem(f"inst-{i}") for i in range(4)]

    async def _drain():
        async for _ in parallel_evaluate(
            problems, intake_service=object(),
            concurrency=2, record_each=False,
            progress_callback=_cb,
        ):
            pass

    asyncio.run(_drain())
    assert len(snapshots) == 4
    # Final snapshot: 4 completed, 0 pending, all PASS.
    final = snapshots[-1]
    assert final.total_completed == 4
    assert final.pending == 0
    assert final.pass_count == 4


def test_progress_callback_exception_is_swallowed(
    clean_env: None, monkeypatch: pytest.MonkeyPatch,
    patch_pipeline: Any,
) -> None:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    def _boom(_snapshot: ParallelEvalProgress) -> None:
        raise RuntimeError("observer_bug")

    problems = [_make_problem("inst-1")]

    async def _drain():
        async for _ in parallel_evaluate(
            problems, intake_service=object(),
            concurrency=1, record_each=False,
            progress_callback=_boom,
        ):
            pass

    asyncio.run(_drain())  # MUST not raise


# ---------------------------------------------------------------------------
# 12. ParallelEvalProgress is frozen
# ---------------------------------------------------------------------------


def test_parallel_eval_progress_is_frozen() -> None:
    p = ParallelEvalProgress(
        total_submitted=1, total_completed=0, pending=1,
        pass_count=0, fail_count=0, partial_count=0,
        error_count=0, skipped_count=0,
        last_instance_id="", last_score_outcome="",
        snapshot_iso="",
    )
    with pytest.raises(Exception):
        p.total_completed = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 13. AST pins - composition discipline
# ---------------------------------------------------------------------------


def _module_source() -> str:
    from backend.core.ouroboros.governance.swe_bench_pro import parallel_eval
    return Path(parallel_eval.__file__).read_text()


def test_ast_pin_imports_canonical_get_semaphore() -> None:
    """Operator binding: composes canonical hot-reload-safe primitive."""
    src = _module_source()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "_process_singletons" in (node.module or ""):
                for alias in node.names:
                    if alias.name == "get_semaphore":
                        found = True
    assert found, (
        "parallel_eval.py does not import canonical get_semaphore "
        "- risk of homegrown semaphore impl"
    )


def test_ast_pin_imports_canonical_pipeline_surfaces() -> None:
    """Composes evaluate_problem (B.2.2) + score_evaluation (Phase C)
    + EvaluationResultStore (Phase D) - no parallel implementations."""
    src = _module_source()
    tree = ast.parse(src)
    needed = {
        "evaluate_problem", "score_evaluation",
        "EvaluationResultStore", "EvaluationRecord",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                needed.discard(alias.name)
    assert not needed, (
        f"parallel_eval.py does not import {sorted(needed)} from "
        f"canonical sources"
    )


def test_ast_pin_no_naked_asyncio_semaphore_literal() -> None:
    """The canonical get_semaphore is the SINGLE seam for bounded
    concurrency. Any `asyncio.Semaphore(` literal in module body
    would re-introduce the hot-reload-orphan bug get_semaphore solves."""
    src = _module_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "Semaphore"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "asyncio"
            ):
                raise AssertionError(
                    "parallel_eval.py constructs asyncio.Semaphore "
                    "directly - use canonical get_semaphore"
                )


def test_ast_pin_no_while_true_loop() -> None:
    """No while-True polling loop. The rig uses bounded queue.get
    sized to the exact number of submitted tasks."""
    src = _module_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.While):
            cond = node.test
            if isinstance(cond, ast.Constant) and cond.value is True:
                raise AssertionError(
                    "parallel_eval.py contains while-True polling loop"
                )


# ---------------------------------------------------------------------------
# 14. FlagRegistry seed
# ---------------------------------------------------------------------------


def test_register_flags_seeds_one_spec() -> None:
    captured: list = []

    class _Capturer:
        def register(self, spec) -> None:
            captured.append(spec)

    count = register_flags(_Capturer())
    assert count == 1
    assert captured[0].name == PARALLEL_CONCURRENCY_ENV_VAR
    assert captured[0].default == 4


def test_register_flags_never_raises_on_capturer_failure() -> None:
    class _Boom:
        def register(self, spec) -> None:
            raise RuntimeError("kaboom")

    assert register_flags(_Boom()) == 0

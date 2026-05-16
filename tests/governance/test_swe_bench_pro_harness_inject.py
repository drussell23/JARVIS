"""Regression spine - SWE-Bench-Pro harness boot hook.

Mirrors the L2 exercise corpus spine pattern at
`tests/governance/test_l2_exercise_seed_harness_wire.py`.

Spine invariants
----------------

  1. Master flag OFF -> SKIPPED_DISABLED (zero substrate calls).
  2. intake_service=None -> FAILED_INJECT.
  3. No cached problems + no CSV -> SKIPPED_NO_PROBLEMS.
  4. CSV override takes priority over count.
  5. Count limits the first-N selection from list_cached_problems.
  6. All loads fail -> FAILED_LOAD.
  7. All ingests return False -> FAILED_INJECT.
  8. Mixed: some succeed -> INJECTED.
  9. Lazy stubbing of prepare_problem ensures no network/git in CI.
 10. Closed 5-value SWEBenchProInjectionVerdict taxonomy.

AST pins
--------

 11. Composes canonical Phase A load_problem.
 12. Composes canonical Phase B.1 prepare_problem.
 13. Composes canonical Phase B.2.1 build_evaluation_envelope.
 14. No homegrown router / no parallel worktree manager.

 15. FlagRegistry: 3 specs; master + count + instance_ids all
     registered; master defaults FALSE per section 33.1.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    LoadOutcome,
    MASTER_FLAG_ENV_VAR as PHASE_A_MASTER,
    ProblemSpec,
)
from backend.core.ouroboros.governance.swe_bench_pro.harness_inject import (
    HARNESS_INJECT_ENABLED_ENV_VAR,
    INJECT_COUNT_ENV_VAR,
    INJECT_INSTANCE_IDS_ENV_VAR,
    SWEBenchProInjectionVerdict,
    maybe_inject_swe_bench_at_boot,
    register_flags,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    HarnessOutcome,
    PreparedProblem,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(HARNESS_INJECT_ENABLED_ENV_VAR, raising=False)
    monkeypatch.delenv(INJECT_COUNT_ENV_VAR, raising=False)
    monkeypatch.delenv(INJECT_INSTANCE_IDS_ENV_VAR, raising=False)
    monkeypatch.delenv(PHASE_A_MASTER, raising=False)
    yield


def _make_problem(instance_id: str) -> ProblemSpec:
    return ProblemSpec(
        instance_id=instance_id,
        repo="org/repo",
        base_commit="abc123",
        problem_statement="stub problem",
        test_patch="",
        gold_patch="",
        repo_url="https://example.com/r.git",
    )


def _make_prepared(instance_id: str, tmp_path: Path) -> PreparedProblem:
    wt = tmp_path / instance_id
    wt.mkdir(exist_ok=True)
    return PreparedProblem(
        problem_instance_id=instance_id,
        worktree_path=wt,
        base_commit="abc123",
        repo_url="https://example.com/r.git",
        branch_name=f"swebp/{instance_id}",
        target_paths=("tests/test_x.py",),
        elapsed_s=0.1,
    )


class _StubIntakeService:
    def __init__(self, return_value: bool = True) -> None:
        self.return_value = return_value
        self.calls: list = []

    async def ingest_envelope(self, envelope) -> bool:
        self.calls.append(envelope)
        return self.return_value


# ---------------------------------------------------------------------------
# 1. Master flag OFF
# ---------------------------------------------------------------------------


def test_master_flag_off_returns_skipped_disabled(clean_env: None) -> None:
    """Master OFF (default) -> SKIPPED_DISABLED, zero side effects."""
    intake = _StubIntakeService()
    verdict = asyncio.run(maybe_inject_swe_bench_at_boot(intake))
    assert verdict == SWEBenchProInjectionVerdict.SKIPPED_DISABLED
    assert intake.calls == []


def test_master_flag_explicit_false(
    monkeypatch: pytest.MonkeyPatch, clean_env: None,
) -> None:
    monkeypatch.setenv(HARNESS_INJECT_ENABLED_ENV_VAR, "false")
    intake = _StubIntakeService()
    verdict = asyncio.run(maybe_inject_swe_bench_at_boot(intake))
    assert verdict == SWEBenchProInjectionVerdict.SKIPPED_DISABLED


# ---------------------------------------------------------------------------
# 2. intake_service=None
# ---------------------------------------------------------------------------


def test_none_intake_service_returns_failed_inject(
    monkeypatch: pytest.MonkeyPatch, clean_env: None,
) -> None:
    monkeypatch.setenv(HARNESS_INJECT_ENABLED_ENV_VAR, "true")
    verdict = asyncio.run(maybe_inject_swe_bench_at_boot(None))
    assert verdict == SWEBenchProInjectionVerdict.FAILED_INJECT


# ---------------------------------------------------------------------------
# 3. No problems available -> SKIPPED_NO_PROBLEMS
# ---------------------------------------------------------------------------


def test_no_problems_returns_skipped_no_problems(
    monkeypatch: pytest.MonkeyPatch, clean_env: None,
) -> None:
    monkeypatch.setenv(HARNESS_INJECT_ENABLED_ENV_VAR, "true")
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.list_cached_problems",
        lambda: (),
    )
    verdict = asyncio.run(maybe_inject_swe_bench_at_boot(
        _StubIntakeService(),
    ))
    assert verdict == SWEBenchProInjectionVerdict.SKIPPED_NO_PROBLEMS


# ---------------------------------------------------------------------------
# 4. CSV override takes priority over count
# ---------------------------------------------------------------------------


def test_csv_override_takes_priority_over_count(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, tmp_path: Path,
) -> None:
    monkeypatch.setenv(HARNESS_INJECT_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(INJECT_INSTANCE_IDS_ENV_VAR, "a-1,b-2")
    monkeypatch.setenv(INJECT_COUNT_ENV_VAR, "99")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.list_cached_problems",
        lambda: ("z-1", "z-2", "z-3"),
    )
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.load_problem",
        lambda iid: (_make_problem(iid), LoadOutcome.LOADED_FROM_CACHE),
    )

    async def _stub_prep(problem):
        return _make_prepared(problem.instance_id, tmp_path), HarnessOutcome.READY
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.prepare_problem", _stub_prep,
    )

    intake = _StubIntakeService()
    verdict = asyncio.run(maybe_inject_swe_bench_at_boot(intake))
    assert verdict == SWEBenchProInjectionVerdict.INJECTED
    # CSV says inject a-1 + b-2 (2 problems), NOT all 99 from count.
    assert len(intake.calls) == 2
    instance_ids = [
        env.evidence["problem_instance_id"] for env in intake.calls
    ]
    assert "a-1" in instance_ids
    assert "b-2" in instance_ids


# ---------------------------------------------------------------------------
# 5. Count limits the first-N selection
# ---------------------------------------------------------------------------


def test_count_limits_first_n(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, tmp_path: Path,
) -> None:
    monkeypatch.setenv(HARNESS_INJECT_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(INJECT_COUNT_ENV_VAR, "2")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.list_cached_problems",
        lambda: ("inst-1", "inst-2", "inst-3", "inst-4"),
    )
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.load_problem",
        lambda iid: (_make_problem(iid), LoadOutcome.LOADED_FROM_CACHE),
    )

    async def _stub_prep(problem):
        return _make_prepared(problem.instance_id, tmp_path), HarnessOutcome.READY
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.prepare_problem", _stub_prep,
    )

    intake = _StubIntakeService()
    verdict = asyncio.run(maybe_inject_swe_bench_at_boot(intake))
    assert verdict == SWEBenchProInjectionVerdict.INJECTED
    assert len(intake.calls) == 2  # First 2 of 4 cached.


# ---------------------------------------------------------------------------
# 6. All loads fail -> FAILED_LOAD
# ---------------------------------------------------------------------------


def test_all_loads_fail_returns_failed_load(
    monkeypatch: pytest.MonkeyPatch, clean_env: None,
) -> None:
    monkeypatch.setenv(HARNESS_INJECT_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(INJECT_INSTANCE_IDS_ENV_VAR, "a-1,b-2")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.load_problem",
        lambda iid: (None, LoadOutcome.MISSING),
    )

    verdict = asyncio.run(maybe_inject_swe_bench_at_boot(
        _StubIntakeService(),
    ))
    assert verdict == SWEBenchProInjectionVerdict.FAILED_LOAD


# ---------------------------------------------------------------------------
# 7. All ingests return False -> FAILED_INJECT
# ---------------------------------------------------------------------------


def test_all_ingests_false_returns_failed_inject(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, tmp_path: Path,
) -> None:
    monkeypatch.setenv(HARNESS_INJECT_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(INJECT_INSTANCE_IDS_ENV_VAR, "a-1")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.load_problem",
        lambda iid: (_make_problem(iid), LoadOutcome.LOADED_FROM_CACHE),
    )

    async def _stub_prep(problem):
        return _make_prepared(problem.instance_id, tmp_path), HarnessOutcome.READY
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.prepare_problem", _stub_prep,
    )

    intake = _StubIntakeService(return_value=False)
    verdict = asyncio.run(maybe_inject_swe_bench_at_boot(intake))
    assert verdict == SWEBenchProInjectionVerdict.FAILED_INJECT
    # ingest_envelope was called (with the envelope) but returned False.
    assert len(intake.calls) == 1


# ---------------------------------------------------------------------------
# 8. Mixed: some succeed -> INJECTED
# ---------------------------------------------------------------------------


def test_partial_success_returns_injected(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, tmp_path: Path,
) -> None:
    monkeypatch.setenv(HARNESS_INJECT_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(INJECT_INSTANCE_IDS_ENV_VAR, "good-1,bad-1,good-2")

    def _stub_load(iid):
        if iid.startswith("bad"):
            return None, LoadOutcome.MISSING
        return _make_problem(iid), LoadOutcome.LOADED_FROM_CACHE

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.load_problem", _stub_load,
    )

    async def _stub_prep(problem):
        return _make_prepared(problem.instance_id, tmp_path), HarnessOutcome.READY
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.prepare_problem", _stub_prep,
    )

    intake = _StubIntakeService()
    verdict = asyncio.run(maybe_inject_swe_bench_at_boot(intake))
    assert verdict == SWEBenchProInjectionVerdict.INJECTED
    assert len(intake.calls) == 2  # Two good, one skipped.


def test_prepare_problem_failure_skips_record(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, tmp_path: Path,
) -> None:
    monkeypatch.setenv(HARNESS_INJECT_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(INJECT_INSTANCE_IDS_ENV_VAR, "a-1,a-2")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.load_problem",
        lambda iid: (_make_problem(iid), LoadOutcome.LOADED_FROM_CACHE),
    )

    async def _stub_prep(problem):
        if problem.instance_id == "a-2":
            return None, HarnessOutcome.CLONE_FAILED
        return _make_prepared(problem.instance_id, tmp_path), HarnessOutcome.READY
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "harness_inject.prepare_problem", _stub_prep,
    )

    intake = _StubIntakeService()
    verdict = asyncio.run(maybe_inject_swe_bench_at_boot(intake))
    assert verdict == SWEBenchProInjectionVerdict.INJECTED
    assert len(intake.calls) == 1  # Only a-1 (a-2 failed prepare).


# ---------------------------------------------------------------------------
# 9. Closed 5-value taxonomy
# ---------------------------------------------------------------------------


def test_verdict_closed_six_value_taxonomy() -> None:
    # Stage 2 added INJECTED_AUTOSCORE (closed-loop outcome); the
    # legacy INJECTED (open-loop) is retained byte-identical.
    values = {v.value for v in SWEBenchProInjectionVerdict}
    assert values == {
        "injected",
        "injected_autoscore",
        "skipped_disabled",
        "skipped_no_problems",
        "failed_load",
        "failed_inject",
    }


# ---------------------------------------------------------------------------
# 10. AST pins - composition discipline
# ---------------------------------------------------------------------------


def _module_source() -> str:
    from backend.core.ouroboros.governance.swe_bench_pro import (
        harness_inject,
    )
    return Path(harness_inject.__file__).read_text()


def test_ast_pin_imports_canonical_load_problem() -> None:
    src = _module_source()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "dataset_loader" in (node.module or ""):
                for alias in node.names:
                    if alias.name == "load_problem":
                        found = True
    assert found, "must import canonical Phase A load_problem"


def test_ast_pin_imports_canonical_prepare_problem() -> None:
    src = _module_source()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "per_problem_harness" in (node.module or ""):
                for alias in node.names:
                    if alias.name == "prepare_problem":
                        found = True
    assert found, "must import canonical Phase B.1 prepare_problem"


def test_ast_pin_imports_canonical_build_evaluation_envelope() -> None:
    src = _module_source()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "envelope_builder" in (node.module or ""):
                for alias in node.names:
                    if alias.name == "build_evaluation_envelope":
                        found = True
    assert found, "must import canonical Phase B.2.1 envelope builder"


def test_ast_pin_no_parallel_worktree_or_router() -> None:
    """Composition discipline: this module imports NO worktree manager
    + NO direct router. All worktree work happens inside Phase B.1's
    prepare_problem; all ingest happens via intake_service.ingest_envelope.

    Walks AST imports + name references (NOT prose docstrings) so
    explanatory comments referencing the forbidden symbols don't trip
    the pin."""
    src = _module_source()
    tree = ast.parse(src)
    forbidden_names = {"WorktreeManager", "UnifiedIntakeRouter"}
    for node in ast.walk(tree):
        # Block imports of the forbidden symbols
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in forbidden_names:
                    raise AssertionError(
                        f"harness_inject imports {alias.name!r} - "
                        f"composition discipline violated"
                    )
        # Block direct name references in expressions
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            raise AssertionError(
                f"harness_inject references {node.id!r} - "
                f"compose Phase B.1 prepare_problem + "
                f"intake_service.ingest_envelope only"
            )


# ---------------------------------------------------------------------------
# 11. FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_register_flags_seeds_four_specs() -> None:
    # Stage 2 added the closed-loop autoscore master switch.
    captured: list = []

    class _Capturer:
        def register(self, spec) -> None:
            captured.append(spec)

    count = register_flags(_Capturer())
    assert count == 4
    names = {s.name for s in captured}
    assert names == {
        HARNESS_INJECT_ENABLED_ENV_VAR,
        INJECT_COUNT_ENV_VAR,
        INJECT_INSTANCE_IDS_ENV_VAR,
        "JARVIS_SWE_BENCH_PRO_AUTOSCORE_ENABLED",
    }


def test_register_flags_master_default_false() -> None:
    captured: list = []

    class _Capturer:
        def register(self, spec) -> None:
            captured.append(spec)

    register_flags(_Capturer())
    master = next(
        s for s in captured
        if s.name == HARNESS_INJECT_ENABLED_ENV_VAR
    )
    assert master.default is False


def test_register_flags_never_raises_on_capturer_failure() -> None:
    class _Boom:
        def register(self, spec) -> None:
            raise RuntimeError("kaboom")

    assert register_flags(_Boom()) == 0


# ---------------------------------------------------------------------------
# 12. Harness wiring - verify the boot block is present in harness.py
# ---------------------------------------------------------------------------


def test_harness_imports_maybe_inject_swe_bench_at_boot() -> None:
    """The battle_test/harness.py boot path imports the boot hook
    lazily inside the SWE-Bench-Pro block."""
    from backend.core.ouroboros.battle_test import harness as harness_mod
    src = Path(harness_mod.__file__).read_text()
    assert "maybe_inject_swe_bench_at_boot" in src, (
        "battle_test/harness.py does not reference the SWE-Bench-Pro "
        "boot hook - wiring missing"
    )
    # And it should be inside a try/except (boot must never fail).
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            text = ast.unparse(node)
            if "maybe_inject_swe_bench_at_boot" in text:
                return
    raise AssertionError(
        "harness.py references maybe_inject_swe_bench_at_boot but NOT "
        "inside a try/except - boot-must-never-fail contract violated"
    )

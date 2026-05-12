"""Treefinement v3.6 Phase 1 — deterministic end-to-end production
wiring exercise.

Closes the wiring-proof gap surfaced by the v3.5 first-soak finding:
the v3.4 production wiring (factory + adapter + lazy boot + archive
+ persistence) is structurally REACHABLE but was never observed
firing under controlled conditions because no op in the existing
battle test infrastructure deliberately fails VALIDATE → reaches L2
→ engages the strategy gate.

This Phase 1 test composes existing Phase 1-E surfaces ONLY (zero
new substrate files, zero new env flags) to drive exactly ONE clean
tree activation in CI. The acceptance predicate is intentionally
minimal + observable: ``.jarvis/ouroboros/repair_tree.jsonl`` in the
test's tmp_path has ≥1 line after ``RepairEngine._maybe_run_treefinement``
returns. A single line means the FULL chain fired end-to-end:

  1. Strategy gate engaged (master flag + strategy + factory checks pass)
  2. Production factory invoked → returned invocation closure
  3. ``RepairTreeRunner.run_tree`` executed
  4. ``CanonicalBranchValidator`` composed (ascii_strict_gate +
     SemanticGuardian + DiffApplier + TestRunner)
  5. Branch produced + archived via ``_archive_result``
  6. ``maybe_archive_tree_result`` → ``persist_tree_result``
  7. ``cross_process_jsonl.flock_append_line`` wrote the line
  8. ``tree_result_to_repair_result`` adapter returned ``RepairResult``

Each failure mode produces an empty file (test fails informatively).

Scope discipline (per v3.6 §40.7.8 PRD design)
----------------------------------------------
* ZERO new substrate files
* ZERO new env flags
* ZERO production behavior change (default-off; test sets env
  in-process via monkeypatch only)
* Composes 8 existing surfaces by reference
* Plus 2 negative-case tests proving byte-identical rollback
  (master-flag-FALSE + LINEAR-strategy)

This test exists FOR wiring proof of life in CI. It does NOT prove
tree mode's actual lift vs LINEAR baseline — that is the Phase 9
graduation criterion, addressed separately by §40.7.9 (Phase 2
SWE-Bench-Pro arc).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend.core.ouroboros.governance.repair_engine import (
    RepairBudget,
    RepairEngine,
)
from backend.core.ouroboros.governance.repair_tree import (
    BranchOutcome,
    DiffApplyResult,
    get_production_tree_runner_factory,
    register_production_tree_runner_factory,
)
from backend.core.ouroboros.governance.repair_tree_archive import (
    reset_default_archive_for_tests,
)
from backend.core.ouroboros.governance.semantic_guardian import (
    SemanticGuardian,
)
from backend.core.ouroboros.governance.test_runner import TestResult


# ===========================================================================
# Test fixtures — stub deps composed via the canonical factory's existing
# injection kwargs. NO new substrate types. NO mocking of internal state.
# ===========================================================================


class _StubWorktreeManager:
    """Composes WorktreeManager Protocol via duck typing. Returns
    a stable tmp dir per branch_name; never invokes git."""

    def __init__(self, base: Path):
        self._base = base

    async def create(self, branch_name: str) -> Path:
        safe = branch_name.replace("/", "_")
        path = self._base / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def cleanup(self, worktree_path: Path) -> None:
        # No-op — tmp_path teardown handles disk cleanup
        return None


class _StubDiffApplier:
    """Composes DiffApplier Protocol. Returns canned ``(path, old, new)``
    tuples without invoking ``git apply``. Models a successful apply
    so the validator's TestRunner gets to run."""

    async def __call__(
        self, *, worktree_dir: Path, diff: str,
    ) -> DiffApplyResult:
        return DiffApplyResult(
            files=(("foo.py", "x = 1\n", "x = 2\n"),),
            error="",
        )


class _StubTestRunner:
    """Composes TestRunner Protocol via duck typing. Returns a
    canned 5/5-pass TestResult so the CanonicalBranchValidator
    scores the branch as WON (forces WON_TERMINAL verdict →
    adapter returns L2_CONVERGED → predictable archive entry)."""

    async def run(
        self, test_files, sandbox_dir=None,
    ) -> TestResult:
        return TestResult(
            passed=True,
            total=5,
            failed=0,
            failed_tests=(),
            duration_seconds=0.01,
            stdout="stub-5-of-5",
            flake_suspected=False,
        )

    async def resolve_affected_tests(self, files):
        return ()


class _StubProvider:
    """Composes prime_provider Protocol via duck typing. Returns
    a synthetically-valid candidate that the production
    BranchGenerator (Phase C) consumes via the canonical
    _generate_repair_candidate (Phase A) primitive."""

    async def generate(self, ctx, pipeline_deadline, *, repair_context):
        class _Result:
            candidates = [{
                "unified_diff": (
                    "--- a/foo.py\n+++ b/foo.py\n"
                    "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
                ),
                "fix_hypothesis": "stub-strategy-for-wiring-proof",
            }]
            model_id = "stub-provider"
            provider_name = "stub"
        return _Result()


class _StubCtx:
    """Minimal OperationContext shape. Composes via attribute access
    only — generator + factory + adapter access op_id / repo_root /
    generation defensively."""

    def __init__(self, *, op_id: str, repo_root: Path):
        self.op_id = op_id
        self.repo_root = repo_root
        self.generation = None


def _make_test_factory(worktree_base: Path):
    """Build a test factory that composes the REAL
    production_tree_runner_factory (Phase D) with stub deps injected
    via the factory's existing kwargs. Phase 1's BranchValidator +
    BranchGenerator + RepairTreeRunner are exercised end-to-end; only
    the FOUR external dependencies (worktree manager / diff applier /
    test runner / semantic guardian) are stubbed.

    Per the operator-override-respecting contract in Phase E's
    ``register_production_factory_at_boot``: registering a custom
    factory bypasses the lazy boot path. The lazy-registration
    pathway is separately pinned in Phase E's hardening test spine
    (23 tests already green). This test exercises the post-
    registration pathway.
    """
    from backend.core.ouroboros.governance.repair_tree_production import (
        production_tree_runner_factory,
    )

    def _factory(
        *, budget, ctx, repair_engine, pipeline_deadline,
        posture=None,
    ):
        return production_tree_runner_factory(
            budget=budget,
            ctx=ctx,
            repair_engine=repair_engine,
            pipeline_deadline=pipeline_deadline,
            posture=posture,
            worktree_manager=_StubWorktreeManager(worktree_base),
            diff_applier=_StubDiffApplier(),
            test_runner=_StubTestRunner(),
            semantic_guardian=SemanticGuardian(),
            max_layers=1,
        )

    return _factory


def _make_engine(tmp_path: Path) -> RepairEngine:
    return RepairEngine(
        budget=RepairBudget(),
        prime_provider=_StubProvider(),
        repo_root=tmp_path,
    )


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch) -> Iterator[None]:
    """Hermetic isolation: env block set in-process; archive +
    factory reset before AND after every test. Production behavior
    is NEVER perturbed — env vars revert when monkeypatch tears down;
    factory is unregistered post-yield."""
    # Canonical env block (matches v3.4 §40.7.7-op soak-readiness
    # checklist exactly — same flags an operator would flip for
    # Phase 9 graduation soaks).
    monkeypatch.setenv("JARVIS_L2_TREEFINEMENT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "bfs")
    monkeypatch.setenv("JARVIS_L2_TREE_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_L2_TREE_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_L2_TREE_PERSISTENCE_PATH",
        str(tmp_path / "repair_tree.jsonl"),
    )

    # Reset substrate singletons
    register_production_tree_runner_factory(None)
    reset_default_archive_for_tests()
    yield
    register_production_tree_runner_factory(None)
    reset_default_archive_for_tests()


# ===========================================================================
# POSITIVE — Phase 1 single acceptance predicate
# ===========================================================================


def test_phase1_acceptance_predicate_jsonl_has_one_line(tmp_path):
    """v3.6 Phase 1 SINGLE ACCEPTANCE PREDICATE: with the canonical
    env block ON and a factory registered, calling
    ``RepairEngine._maybe_run_treefinement`` MUST result in
    ``repair_tree.jsonl`` (at the persistence-path env var) having
    ≥1 line.

    This single observable proves the full chain fired end-to-end:

      1. Strategy gate engaged (treefinement_enabled + strategy != LINEAR
         + factory registered all returned truthy)
      2. ``_invoke_tree_factory`` stage 1: factory constructed closure
      3. Stage 2: closure awaited → RepairTreeRunner.run_tree executed
      4. CanonicalBranchValidator scored the branch → WON outcome
         (score 1.0 from canned 5/5 TestRunner pass + zero Guardian
         findings)
      5. WON_TERMINAL verdict → ``_archive_result`` fired
      6. ``maybe_archive_tree_result`` → ``persist_tree_result``
      7. ``cross_process_jsonl.flock_append_line`` succeeded
      8. Stage 3 adapter: ``tree_result_to_repair_result`` returned
         ``L2_CONVERGED`` RepairResult

    A failure at ANY of the 8 steps produces an empty file (or no
    file at all) → test fails informatively. The predicate is the
    most-observable single signal that the v3.4 production wiring
    is end-to-end intact.
    """
    register_production_tree_runner_factory(
        _make_test_factory(tmp_path),
    )

    engine = _make_engine(tmp_path)
    ctx = _StubCtx(op_id="op-e2e-phase1-positive", repo_root=tmp_path)

    result = asyncio.run(engine._maybe_run_treefinement(
        ctx, None, datetime.now(timezone.utc),
    ))

    # Stage 1+2+3 of the gate's 3-stage pipeline all succeeded
    assert result is not None, (
        "gate MUST return a RepairResult when all 3 conditions "
        "(master flag + strategy + factory) are met"
    )
    assert result.terminal == "L2_CONVERGED", (
        f"WON_TERMINAL verdict should map to L2_CONVERGED via the "
        f"Phase D adapter; got terminal={result.terminal!r} "
        f"stop_reason={result.stop_reason!r}"
    )

    # ACCEPTANCE PREDICATE
    jsonl_path = tmp_path / "repair_tree.jsonl"
    assert jsonl_path.exists(), (
        "PHASE 1 ACCEPTANCE PREDICATE FAILED: "
        f"{jsonl_path} does NOT exist. The full production-wiring "
        "chain did not complete: missing the persist_tree_result "
        "step OR earlier failure prevented archive write."
    )
    lines = [
        line for line in jsonl_path.read_text(encoding="utf-8").split("\n")
        if line.strip()
    ]
    assert len(lines) >= 1, (
        "PHASE 1 ACCEPTANCE PREDICATE FAILED: "
        f"repair_tree.jsonl has {len(lines)} lines (expected ≥1). "
        "The persist_tree_result call ran but produced no row — "
        "indicates a bug in the persistence path (Phase 4 wire)."
    )


# ===========================================================================
# NEGATIVE #1 — master-flag-FALSE rollback (byte-identical)
# ===========================================================================


def test_phase1_master_flag_false_no_jsonl(tmp_path, monkeypatch):
    """v3.6 Phase 1 NEGATIVE CASE #1: master-flag-FALSE → gate
    returns None → caller falls through to legacy ``_run_inner``
    byte-identically.

    Rolling back the master flag MUST prevent ANY tree-mode artifact
    creation. This pins the v3.4 §40.7.7-op rollback contract:
    ``JARVIS_L2_TREEFINEMENT_ENABLED=false`` is the master kill;
    gate exits before any tree-mode code path executes.
    """
    # Override the env block: master flag OFF (operator soft rollback)
    monkeypatch.delenv("JARVIS_L2_TREEFINEMENT_ENABLED", raising=False)

    register_production_tree_runner_factory(
        _make_test_factory(tmp_path),
    )

    engine = _make_engine(tmp_path)
    ctx = _StubCtx(op_id="op-e2e-phase1-master-off", repo_root=tmp_path)

    result = asyncio.run(engine._maybe_run_treefinement(
        ctx, None, datetime.now(timezone.utc),
    ))

    assert result is None, (
        "ROLLBACK CONTRACT VIOLATED: gate MUST return None when "
        f"master flag is FALSE; got result={result}"
    )

    jsonl_path = tmp_path / "repair_tree.jsonl"
    assert not jsonl_path.exists(), (
        f"ROLLBACK CONTRACT VIOLATED: {jsonl_path} exists. "
        "Master-FALSE MUST prevent ANY tree-mode artifact creation."
    )


# ===========================================================================
# NEGATIVE #2 — LINEAR-strategy rollback (operator-chosen fall-through)
# ===========================================================================


def test_phase1_linear_strategy_no_jsonl(tmp_path, monkeypatch):
    """v3.6 Phase 1 NEGATIVE CASE #2: strategy=LINEAR → gate
    returns None → caller falls through to ``_run_inner``.

    LINEAR is the byte-identical legacy strategy. Operators flip to
    LINEAR for immediate rollback without disabling tree-mode
    infrastructure. This pins the operator-soft-rollback path.
    """
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "linear")

    register_production_tree_runner_factory(
        _make_test_factory(tmp_path),
    )

    engine = _make_engine(tmp_path)
    ctx = _StubCtx(op_id="op-e2e-phase1-linear", repo_root=tmp_path)

    result = asyncio.run(engine._maybe_run_treefinement(
        ctx, None, datetime.now(timezone.utc),
    ))

    assert result is None, (
        "ROLLBACK CONTRACT VIOLATED: gate MUST return None when "
        f"strategy=LINEAR; got result={result}"
    )

    jsonl_path = tmp_path / "repair_tree.jsonl"
    assert not jsonl_path.exists(), (
        f"ROLLBACK CONTRACT VIOLATED: {jsonl_path} exists. "
        "LINEAR strategy MUST prevent ANY tree-mode artifact creation."
    )


# ===========================================================================
# POSITIVE — verify the gate also exercises archive ring (in-memory)
# ===========================================================================


def test_phase1_archive_ring_also_populated(tmp_path):
    """Defense in depth: the positive-case acceptance predicate
    asserts JSONL persistence. This test additionally asserts the
    in-memory archive ring (Phase 4 substrate) is populated — both
    surfaces compose via the canonical ``maybe_archive_tree_result``
    producer-bridge, so both should fire from a single tree run."""
    from backend.core.ouroboros.governance.repair_tree_archive import (
        get_default_archive,
    )

    register_production_tree_runner_factory(
        _make_test_factory(tmp_path),
    )

    engine = _make_engine(tmp_path)
    ctx = _StubCtx(op_id="op-e2e-phase1-archive", repo_root=tmp_path)

    asyncio.run(engine._maybe_run_treefinement(
        ctx, None, datetime.now(timezone.utc),
    ))

    archive = get_default_archive()
    snapshot = archive.snapshot()
    assert snapshot.size >= 1, (
        f"In-memory archive ring has size={snapshot.size}; "
        "expected ≥1 after a tree run completes. Either the "
        "_archive_result helper did not fire OR the ring's master "
        "flag short-circuited the record_result call."
    )

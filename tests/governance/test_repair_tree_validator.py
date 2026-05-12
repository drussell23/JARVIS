"""Regression spine for Treefinement Phase 2 — CanonicalBranchValidator.

Pins the structural invariants for the per-branch pruning oracle:

* Stage discipline (cheapest first): ASCII → diff apply → Guardian
  → TestRunner. Each stage NEVER raises into the runner.
* Hard SemanticGuardian findings short-circuit to PRUNED_VALIDATOR
  with SEMANTIC_GUARDIAN_HARD_FINDING.
* Non-ASCII codepoints short-circuit to PRUNED_VALIDATOR with
  IRON_GATE_REJECT.
* Soft findings reduce validator_score but do NOT prune.
* WON requires both score ≥ won_score_floor AND zero soft findings
  (load-bearing strict-WON invariant — single soft finding demotes
  to PROMOTED).
* Composition AST pins (ascii_strict_gate / SemanticGuardian /
  TestRunner imports) — drift toward inline pattern detection or
  parallel test infrastructure is structurally forbidden.
"""
from __future__ import annotations

import asyncio
import ast
import inspect
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance import repair_tree
from backend.core.ouroboros.governance.repair_tree import (
    PROMOTED_SCORE_FLOOR_ENV_VAR,
    SOFT_FINDING_PENALTY_ENV_VAR,
    TEST_PASS_WEIGHT_ENV_VAR,
    TEST_TIMEOUT_S_ENV_VAR,
    WON_SCORE_FLOOR_ENV_VAR,
    BranchOutcome,
    CanonicalBranchValidator,
    DiffApplyResult,
    PruningReason,
    ValidatorScoringConfig,
)


# ===========================================================================
# Stub fixtures — the four injection points used across all tests
# ===========================================================================


class _StubAppliedTuples:
    """Canned (path, old, new) tuples for SemanticGuardian inspection."""

    @staticmethod
    def empty() -> Tuple[Tuple[str, str, str], ...]:
        return ()

    @staticmethod
    def one_file() -> Tuple[Tuple[str, str, str], ...]:
        return (("foo.py", "x = 1\n", "x = 2\n"),)

    @staticmethod
    def two_files() -> Tuple[Tuple[str, str, str], ...]:
        return (
            ("foo.py", "x = 1\n", "x = 2\n"),
            ("bar.py", "y = 1\n", "y = 2\n"),
        )


def _stub_applier(
    *,
    files: Optional[Tuple[Tuple[str, str, str], ...]] = None,
    error: str = "",
    raises: Optional[BaseException] = None,
):
    """Build a stub DiffApplier returning canned files / error."""
    captured = {"call_count": 0, "args": []}

    async def _apply(*, worktree_dir: Path, diff: str):
        captured["call_count"] += 1
        captured["args"].append({"worktree_dir": worktree_dir, "diff": diff})
        if raises is not None:
            raise raises
        return DiffApplyResult(
            files=files if files is not None else (),
            error=error,
        )

    _apply.captured = captured  # type: ignore[attr-defined]
    return _apply


def _stub_resolver(
    *,
    targets: Tuple[Path, ...] = (),
    raises: Optional[BaseException] = None,
):
    async def _resolve(**_kwargs):
        if raises is not None:
            raise raises
        return targets

    return _resolve


class _StubTestRunner:
    """Implements TestRunner-compatible ``.run`` returning a canned
    TestResult."""

    def __init__(
        self,
        *,
        passed: bool = True,
        total: int = 5,
        failed: int = 0,
        raises: Optional[BaseException] = None,
        flake_suspected: bool = False,
    ):
        self.passed = passed
        self.total = total
        self.failed = failed
        self.raises = raises
        self.flake_suspected = flake_suspected
        self.calls: List[dict] = []

    async def run(self, test_files, sandbox_dir=None):
        self.calls.append({
            "test_files": test_files,
            "sandbox_dir": sandbox_dir,
        })
        if self.raises is not None:
            raise self.raises
        from backend.core.ouroboros.governance.test_runner import (
            TestResult,
        )
        return TestResult(
            passed=self.passed,
            total=self.total,
            failed=self.failed,
            failed_tests=tuple(
                f"test_{i}" for i in range(self.failed)
            ),
            duration_seconds=0.01,
            stdout="stub-output",
            flake_suspected=self.flake_suspected,
        )


class _StubGuardian:
    """SemanticGuardian-compatible inspect_batch returning canned
    severities."""

    def __init__(
        self,
        *,
        hard_count: int = 0,
        soft_count: int = 0,
        raises: Optional[BaseException] = None,
    ):
        self.hard_count = hard_count
        self.soft_count = soft_count
        self.raises = raises
        self.calls: List[Any] = []

    def inspect_batch(self, candidates):
        self.calls.append(tuple(candidates))
        if self.raises is not None:
            raise self.raises
        from backend.core.ouroboros.governance.semantic_guardian import (
            Detection,
        )
        out: List[Detection] = []
        for i in range(self.hard_count):
            out.append(Detection(
                pattern="hard-test", severity="hard",
                message=f"hard-{i}", file_path="foo.py",
            ))
        for i in range(self.soft_count):
            out.append(Detection(
                pattern="soft-test", severity="soft",
                message=f"soft-{i}", file_path="foo.py",
            ))
        return out


def _stub_ascii_check(
    *,
    offender_count: int = 0,
    raises: Optional[BaseException] = None,
):
    def _check(_diff: str) -> List[Any]:
        if raises is not None:
            raise raises
        from backend.core.ouroboros.governance.ascii_strict_gate import (
            BadCodepoint,
        )
        return [
            BadCodepoint(
                file_path="?", offset=i, char="é",
                codepoint=0x00E9, line=1, column=i + 1,
            )
            for i in range(offender_count)
        ]
    return _check


def _make_validator(
    *,
    applier=None,
    test_runner=None,
    resolver=None,
    guardian=None,
    ascii_check=None,
    scoring=None,
):
    """Construct a CanonicalBranchValidator with stub defaults."""
    return CanonicalBranchValidator(
        diff_applier=applier or _stub_applier(
            files=_StubAppliedTuples.one_file(),
        ),
        test_runner=test_runner or _StubTestRunner(),  # type: ignore[arg-type]
        test_target_resolver=resolver or _stub_resolver(),
        semantic_guardian=guardian or _StubGuardian(),  # type: ignore[arg-type]
        ascii_check=ascii_check or _stub_ascii_check(),
        scoring=scoring,
    )


def _invoke(validator) -> Tuple[BranchOutcome, float, Optional[PruningReason], int]:
    return asyncio.run(validator(
        op_id="op-test",
        branch_id="b" * 16,
        diff="--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
        worktree_dir=Path("/tmp/wt"),
    ))


# ===========================================================================
# ValidatorScoringConfig — env loader (NEVER raises)
# ===========================================================================


def test_scoring_config_defaults(monkeypatch):
    for k in [
        TEST_PASS_WEIGHT_ENV_VAR,
        SOFT_FINDING_PENALTY_ENV_VAR,
        WON_SCORE_FLOOR_ENV_VAR,
        PROMOTED_SCORE_FLOOR_ENV_VAR,
        TEST_TIMEOUT_S_ENV_VAR,
    ]:
        monkeypatch.delenv(k, raising=False)
    cfg = ValidatorScoringConfig.from_env()
    assert cfg.test_pass_weight == 1.0
    assert cfg.soft_finding_penalty == 0.2
    assert cfg.won_score_floor == 0.95
    assert cfg.promoted_score_floor == 0.4
    assert cfg.test_timeout_s == 60.0


def test_scoring_config_env_overrides(monkeypatch):
    monkeypatch.setenv(TEST_PASS_WEIGHT_ENV_VAR, "2.5")
    monkeypatch.setenv(SOFT_FINDING_PENALTY_ENV_VAR, "0.5")
    monkeypatch.setenv(WON_SCORE_FLOOR_ENV_VAR, "1.5")
    monkeypatch.setenv(PROMOTED_SCORE_FLOOR_ENV_VAR, "0.6")
    monkeypatch.setenv(TEST_TIMEOUT_S_ENV_VAR, "120.0")
    cfg = ValidatorScoringConfig.from_env()
    assert cfg.test_pass_weight == 2.5
    assert cfg.soft_finding_penalty == 0.5
    assert cfg.won_score_floor == 1.5
    assert cfg.promoted_score_floor == 0.6
    assert cfg.test_timeout_s == 120.0


def test_scoring_config_clamps_apply(monkeypatch):
    monkeypatch.setenv(TEST_PASS_WEIGHT_ENV_VAR, "-5.0")
    monkeypatch.setenv(SOFT_FINDING_PENALTY_ENV_VAR, "999.0")
    monkeypatch.setenv(TEST_TIMEOUT_S_ENV_VAR, "0.001")
    cfg = ValidatorScoringConfig.from_env()
    assert cfg.test_pass_weight == 0.0  # min 0
    assert cfg.soft_finding_penalty == 10.0  # max 10
    assert cfg.test_timeout_s == 1.0  # min 1


def test_scoring_config_handles_garbage(monkeypatch):
    """Adversarial env state — every knob malformed."""
    for k in [
        TEST_PASS_WEIGHT_ENV_VAR,
        SOFT_FINDING_PENALTY_ENV_VAR,
        WON_SCORE_FLOOR_ENV_VAR,
        PROMOTED_SCORE_FLOOR_ENV_VAR,
        TEST_TIMEOUT_S_ENV_VAR,
    ]:
        monkeypatch.setenv(k, "elephant")
    cfg = ValidatorScoringConfig.from_env()
    # All defaults preserved
    assert cfg.test_pass_weight == 1.0
    assert cfg.test_timeout_s == 60.0


# ===========================================================================
# Stage 1 — ASCII gate (binary, immediate short-circuit)
# ===========================================================================


def test_validator_ascii_offender_returns_iron_gate_reject():
    validator = _make_validator(
        ascii_check=_stub_ascii_check(offender_count=3),
    )
    outcome, score, reason, runs = _invoke(validator)
    assert outcome == BranchOutcome.PRUNED_VALIDATOR
    assert score == 0.0
    assert reason == PruningReason.IRON_GATE_REJECT
    assert runs == 0


def test_validator_ascii_check_exception_treated_as_iron_gate_reject():
    """Defensive: ascii_check raise MUST short-circuit to
    IRON_GATE_REJECT (fail-CLOSED for safety gate)."""
    validator = _make_validator(
        ascii_check=_stub_ascii_check(raises=RuntimeError("ascii broke")),
    )
    outcome, score, reason, _ = _invoke(validator)
    assert outcome == BranchOutcome.PRUNED_VALIDATOR
    assert reason == PruningReason.IRON_GATE_REJECT


def test_validator_ascii_clean_proceeds_to_apply():
    """ASCII clean → apply stage runs → tests gather verdict."""
    applier = _stub_applier(files=_StubAppliedTuples.one_file())
    validator = _make_validator(applier=applier)
    _invoke(validator)
    assert applier.captured["call_count"] == 1


# ===========================================================================
# Stage 2 — DiffApplier (apply failure → PRUNED, never falls back)
# ===========================================================================


def test_validator_apply_error_returns_pruned_worse_than_sibling():
    validator = _make_validator(
        applier=_stub_applier(error="patch_failed: malformed hunk"),
    )
    outcome, score, reason, _ = _invoke(validator)
    assert outcome == BranchOutcome.PRUNED_VALIDATOR
    assert score == 0.0
    assert reason == PruningReason.WORSE_THAN_SIBLING


def test_validator_apply_protocol_violation_quarantines():
    """DiffApplier MUST NEVER raise per Protocol — but if it does
    (defense in depth), validator quarantines to PRUNED_VALIDATOR."""
    validator = _make_validator(
        applier=_stub_applier(raises=RuntimeError("applier broke")),
    )
    outcome, _, reason, _ = _invoke(validator)
    assert outcome == BranchOutcome.PRUNED_VALIDATOR
    assert reason == PruningReason.WORSE_THAN_SIBLING


def test_validator_apply_success_proceeds_to_guardian():
    guardian = _StubGuardian(hard_count=0, soft_count=0)
    applier = _stub_applier(files=_StubAppliedTuples.two_files())
    validator = _make_validator(applier=applier, guardian=guardian)
    _invoke(validator)
    assert len(guardian.calls) == 1
    # Guardian receives the SAME tuples DiffApplier returned (no re-parse)
    assert guardian.calls[0] == _StubAppliedTuples.two_files()


# ===========================================================================
# Stage 3 — SemanticGuardian (hard short-circuits, soft reduces score)
# ===========================================================================


def test_validator_hard_finding_short_circuits():
    """Hard finding → immediate PRUNED_VALIDATOR with
    SEMANTIC_GUARDIAN_HARD_FINDING. Tests NEVER invoked."""
    runner = _StubTestRunner()
    validator = _make_validator(
        guardian=_StubGuardian(hard_count=1),
        test_runner=runner,
    )
    outcome, score, reason, _ = _invoke(validator)
    assert outcome == BranchOutcome.PRUNED_VALIDATOR
    assert reason == PruningReason.SEMANTIC_GUARDIAN_HARD_FINDING
    assert score == 0.0
    assert len(runner.calls) == 0, (
        "TestRunner MUST NOT be invoked when Guardian short-circuits"
    )


def test_validator_soft_findings_reduce_score_not_prune():
    """One soft finding + clean tests = score = 1.0 - 0.2 = 0.8 →
    PROMOTED (above floor 0.4, below WON 0.95)."""
    validator = _make_validator(
        guardian=_StubGuardian(hard_count=0, soft_count=1),
        test_runner=_StubTestRunner(passed=True, total=5, failed=0),
    )
    outcome, score, reason, _ = _invoke(validator)
    assert outcome == BranchOutcome.PROMOTED
    assert reason is None
    assert abs(score - 0.8) < 1e-6


def test_validator_guardian_exception_treated_as_no_findings():
    """Guardian.inspect_batch raise MUST NOT crash the validator —
    defensive treat-as-no-findings (Guardian.inspect already swallows
    per-pattern exceptions internally; this catches the outer call)."""
    validator = _make_validator(
        guardian=_StubGuardian(raises=RuntimeError("guardian broke")),
        test_runner=_StubTestRunner(passed=True, total=5, failed=0),
    )
    outcome, score, _, _ = _invoke(validator)
    # Treated as no findings → clean test pass → score = 1.0
    assert outcome == BranchOutcome.WON
    assert score == 1.0


# ===========================================================================
# Stage 4 — TestRunner (score formula + outcome mapping)
# ===========================================================================


def test_validator_clean_tests_no_findings_yields_won():
    """5/5 tests pass + 0 findings → score 1.0 ≥ 0.95 floor → WON."""
    validator = _make_validator(
        test_runner=_StubTestRunner(passed=True, total=5, failed=0),
        guardian=_StubGuardian(),
    )
    outcome, score, reason, runs = _invoke(validator)
    assert outcome == BranchOutcome.WON
    assert score == 1.0
    assert reason is None
    assert runs == 1


def test_validator_clean_tests_one_soft_demotes_to_promoted():
    """Strict-WON invariant: even with score ≥ won_floor, ANY soft
    finding demotes WON → PROMOTED. This is the load-bearing
    'WON requires clean signal' contract."""
    validator = _make_validator(
        test_runner=_StubTestRunner(passed=True, total=5, failed=0),
        guardian=_StubGuardian(soft_count=1),
    )
    outcome, score, _, _ = _invoke(validator)
    # Score = 1.0 - 0.2 = 0.8, which is < 0.95 anyway, so PROMOTED.
    # But even at score=1.0 with soft_count>0, MUST be PROMOTED.
    assert outcome == BranchOutcome.PROMOTED


def test_validator_strict_won_with_inflated_score(monkeypatch):
    """With test_pass_weight=2.0, score = 2.0 ≥ 0.95 won_floor.
    But if soft_count > 0, MUST still be PROMOTED (zero-soft is
    a hard requirement orthogonal to the score floor)."""
    monkeypatch.setenv(TEST_PASS_WEIGHT_ENV_VAR, "2.0")
    cfg = ValidatorScoringConfig.from_env()
    validator = _make_validator(
        test_runner=_StubTestRunner(passed=True, total=5, failed=0),
        guardian=_StubGuardian(soft_count=1),
        scoring=cfg,
    )
    outcome, score, _, _ = _invoke(validator)
    # Score = 2.0 - 0.2 = 1.8 (well above won_floor of 0.95)
    assert score > 0.95
    assert outcome == BranchOutcome.PROMOTED, (
        "Strict-WON: score above floor BUT soft>0 → PROMOTED"
    )


def test_validator_partial_test_pass_yields_promoted():
    """3/5 pass = ratio 0.6 → PROMOTED (above 0.4 floor)."""
    validator = _make_validator(
        test_runner=_StubTestRunner(passed=False, total=5, failed=2),
    )
    outcome, score, _, _ = _invoke(validator)
    assert abs(score - 0.6) < 1e-6
    assert outcome == BranchOutcome.PROMOTED


def test_validator_low_test_pass_yields_pruned_worse_than_sibling():
    """1/5 pass = ratio 0.2 → below promoted floor 0.4 →
    PRUNED_VALIDATOR with WORSE_THAN_SIBLING."""
    validator = _make_validator(
        test_runner=_StubTestRunner(passed=False, total=5, failed=4),
    )
    outcome, score, reason, _ = _invoke(validator)
    assert abs(score - 0.2) < 1e-6
    assert outcome == BranchOutcome.PRUNED_VALIDATOR
    assert reason == PruningReason.WORSE_THAN_SIBLING


def test_validator_all_tests_fail_yields_pruned():
    validator = _make_validator(
        test_runner=_StubTestRunner(passed=False, total=5, failed=5),
    )
    outcome, score, reason, _ = _invoke(validator)
    assert score == 0.0
    assert outcome == BranchOutcome.PRUNED_VALIDATOR
    assert reason == PruningReason.WORSE_THAN_SIBLING


def test_validator_zero_total_tests_treated_as_vacuous_pass():
    """No relevant tests → ratio 0/1 = 0 → PRUNED unless
    score-formula-floor=0. Default formula: ratio 0 → score 0 → PRUNED.
    This is the conservative semantic — 'no test evidence' should not
    be PROMOTED. Operators can flip with PROMOTED_SCORE_FLOOR=0."""
    validator = _make_validator(
        test_runner=_StubTestRunner(passed=True, total=0, failed=0),
    )
    outcome, score, _, _ = _invoke(validator)
    assert score == 0.0
    assert outcome == BranchOutcome.PRUNED_VALIDATOR


def test_validator_test_runner_exception_quarantines():
    validator = _make_validator(
        test_runner=_StubTestRunner(raises=RuntimeError("pytest broke")),
    )
    outcome, _, reason, _ = _invoke(validator)
    assert outcome == BranchOutcome.PRUNED_VALIDATOR
    assert reason == PruningReason.WORSE_THAN_SIBLING


def test_validator_resolver_exception_treated_as_empty_targets():
    """Resolver raise → empty targets → TestRunner sees vacuous pass
    → score 0 → PRUNED. Defensive: resolver crash MUST NOT crash
    validator."""
    validator = _make_validator(
        resolver=_stub_resolver(raises=RuntimeError("resolver broke")),
        test_runner=_StubTestRunner(passed=True, total=0, failed=0),
    )
    outcome, score, _, _ = _invoke(validator)
    # Doesn't crash; outcome reflects vacuous pass
    assert outcome == BranchOutcome.PRUNED_VALIDATOR
    assert score == 0.0


# ===========================================================================
# Cancellation propagation (§1 Boundary)
# ===========================================================================


def test_validator_cancellation_from_apply_propagates():
    validator = _make_validator(
        applier=_stub_applier(raises=asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        _invoke(validator)


def test_validator_cancellation_from_test_runner_propagates():
    validator = _make_validator(
        test_runner=_StubTestRunner(raises=asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        _invoke(validator)


def test_validator_cancellation_from_ascii_check_propagates():
    validator = _make_validator(
        ascii_check=_stub_ascii_check(raises=asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        _invoke(validator)


# ===========================================================================
# End-to-end with Phase 1 RepairTreeRunner
# ===========================================================================


def test_validator_integrates_with_runner_yields_won_terminal(monkeypatch):
    """Wire CanonicalBranchValidator into Phase 1 runner. Verify
    end-to-end: clean tests + no findings → WON_TERMINAL layer."""
    from backend.core.ouroboros.governance.repair_tree import (
        MASTER_FLAG_ENV_VAR,
        BranchingStrategy,
        LayerVerdict,
        RepairTreeRunner,
        TreefinementBudget,
    )

    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    budget = TreefinementBudget(
        enabled=True,
        branching_strategy=BranchingStrategy.BFS,
        max_branches_per_layer=2,
        beam_width=2,
        branch_dedup_enabled=True,
        cross_branch_learning_enabled=True,
        emergency_demote_threshold=0.85,
    )
    runner = RepairTreeRunner(budget, worktree_manager=None)

    validator = _make_validator(
        test_runner=_StubTestRunner(passed=True, total=5, failed=0),
    )

    async def _generator(*, op_id, layer_index, parent_branch, sibling_outcomes):
        return ("--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n", "fix-rename", 0.001)

    result = asyncio.run(runner.run_tree(
        op_id="op-e2e",
        generator=_generator,
        validator=validator,
        max_layers=1,
    ))
    assert len(result.layers) == 1
    assert result.layers[0].verdict == LayerVerdict.WON_TERMINAL
    won = next(
        b for b in result.layers[0].branches
        if b.outcome == BranchOutcome.WON
    )
    assert won.validator_score == 1.0


def test_validator_integrates_with_runner_yields_exhausted(monkeypatch):
    """End-to-end: ascii offenders on every branch → all
    IRON_GATE_REJECT → EXHAUSTED layer."""
    from backend.core.ouroboros.governance.repair_tree import (
        MASTER_FLAG_ENV_VAR,
        BranchingStrategy,
        LayerVerdict,
        RepairTreeRunner,
        TreefinementBudget,
    )

    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    budget = TreefinementBudget(
        enabled=True,
        branching_strategy=BranchingStrategy.BFS,
        max_branches_per_layer=2,
        beam_width=2,
        branch_dedup_enabled=True,
        cross_branch_learning_enabled=True,
        emergency_demote_threshold=0.85,
    )
    runner = RepairTreeRunner(budget, worktree_manager=None)

    validator = _make_validator(
        ascii_check=_stub_ascii_check(offender_count=1),
    )

    # Counter-based unique diffs (id(None) at layer 0 collapses if
    # we use parent_branch as the seed — counter is the safe choice).
    call_counter = {"n": 0}

    async def _generator(**_kwargs):
        call_counter["n"] += 1
        return (
            f"--- a\n+++ b\n@@ -1 +1 @@\n-x\n+unique-{call_counter['n']}\n",
            "fix",
            0.001,
        )

    result = asyncio.run(runner.run_tree(
        op_id="op-e2e-exhaust",
        generator=_generator,
        validator=validator,
        max_layers=1,
    ))
    layer = result.layers[0]
    assert layer.verdict == LayerVerdict.EXHAUSTED
    assert all(
        b.outcome == BranchOutcome.PRUNED_VALIDATOR
        and b.prune_reason == PruningReason.IRON_GATE_REJECT
        for b in layer.branches
    )


# ===========================================================================
# AST composition pins (single-source-of-truth for validator stack)
# ===========================================================================


_MODULE_SRC = Path(inspect.getfile(repair_tree)).read_text(encoding="utf-8")
_MODULE_AST = ast.parse(_MODULE_SRC)


def _imports() -> List[Tuple[str, Tuple[str, ...]]]:
    out = []
    for node in ast.walk(_MODULE_AST):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = tuple(a.name for a in node.names)
            out.append((mod, names))
    return out


def test_composition_pin_ascii_strict_gate():
    matches = [
        (m, n) for (m, n) in _imports()
        if m.endswith("ascii_strict_gate") and "scan_content" in n
    ]
    assert matches, (
        "repair_tree.py MUST import scan_content from "
        "ascii_strict_gate — composition pin"
    )


def test_composition_pin_semantic_guardian():
    matches = [
        (m, n) for (m, n) in _imports()
        if m.endswith("semantic_guardian") and "SemanticGuardian" in n
    ]
    assert matches, (
        "repair_tree.py MUST import SemanticGuardian — "
        "composition pin (no parallel pattern detector)"
    )


def test_composition_pin_test_runner():
    matches = [
        (m, n) for (m, n) in _imports()
        if m.endswith("test_runner")
        and "TestRunner" in n
        and "TestResult" in n
    ]
    assert matches, (
        "repair_tree.py MUST import TestRunner + TestResult — "
        "composition pin (no parallel pytest infrastructure)"
    )


def test_validator_class_does_not_define_inline_pattern_detector():
    """No method on CanonicalBranchValidator may name itself
    *_pattern* / *_detect* — that's the parallel-detector anti-pattern."""
    for node in ast.walk(_MODULE_AST):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "CanonicalBranchValidator"
        ):
            for stmt in ast.walk(node):
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = stmt.name.lower()
                    assert "pattern" not in name, (
                        f"method {stmt.name!r} suggests parallel "
                        "pattern detector — compose SemanticGuardian"
                    )
                    assert "detect" not in name, (
                        f"method {stmt.name!r} suggests parallel "
                        "detector — compose SemanticGuardian"
                    )


def test_validator_class_does_not_define_inline_test_runner():
    """No method may invoke pytest directly — TestRunner is the
    canonical test-execution surface."""
    for node in ast.walk(_MODULE_AST):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "CanonicalBranchValidator"
        ):
            for stmt in ast.walk(node):
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = stmt.name.lower()
                    assert "pytest" not in name, (
                        f"method {stmt.name!r} suggests inline pytest "
                        "invocation — compose TestRunner"
                    )

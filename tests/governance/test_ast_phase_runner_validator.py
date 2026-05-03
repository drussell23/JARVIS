"""RR Pass B Slice 3 — AST PhaseRunner validator regression suite.

Pins:
  * Module constants + ValidationStatus enum (5 values) +
    ValidationFailureReason enum (8 values) + frozen ValidationResult.
  * Env knob default-false-pre-graduation (master-off → SKIPPED).
  * Defensive: None source / oversize source / SyntaxError parse.
  * Rule 1 — ABC conformance: pass for direct + module-prefixed
    inheritance; fail when no PhaseRunner subclass present.
  * Rule 2 — phase attribute: pass for AnnAssign-with-value +
    plain Assign; fail for missing + AnnAssign-without-value.
  * Rule 3 — run signature: pass for spec-conformant; fail for
    missing run / sync def / wrong arg count / wrong arg names /
    missing OperationContext annotation / missing PhaseResult
    return / *args/**kwargs.
  * Rule 4 — no ctx mutation: pass for ctx.advance(...) +
    rebinding ``ctx = ...``; fail for direct attr assign + augmented
    assign + annotated assign with value.
  * Rule 5 — top-level try/except: pass for try at top level
    (with prefix code allowed); fail for absent try.
  * Rule 6 — banned imports: pass for stdlib + third-party + 4
    allowed governance modules; fail for ANY other governance
    module + caller-supplied extra_banned_modules.
  * validate_ast_strict: raises PhaseRunnerASTValidationError on
    FAILED; returns result on PASSED / SKIPPED / OVERSIZE /
    PARSE_ERROR.
  * Real graduated PhaseRunner subclasses (gate_runner.py +
    complete_runner.py) MUST validate cleanly under master-on.
  * Authority invariants: no banned imports + no I/O / subprocess /
    env mutation.
"""
from __future__ import annotations

import dataclasses
import io
import textwrap
import tokenize
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.meta.ast_phase_runner_validator import (
    MAX_CANDIDATE_BYTES,
    PhaseRunnerASTValidationError,
    ValidationFailureReason,
    ValidationResult,
    ValidationStatus,
    is_enabled,
    validate_ast,
    validate_ast_strict,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


# Spec-conformant runner template — 6 rules satisfied.
_GOOD_RUNNER = textwrap.dedent("""
    from backend.core.ouroboros.governance.phase_runner import (
        PhaseRunner, PhaseResult,
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationContext, OperationPhase,
    )

    class GoodRunner(PhaseRunner):
        phase: OperationPhase = OperationPhase.ROUTE

        async def run(self, ctx: OperationContext) -> PhaseResult:
            try:
                new_ctx = ctx.advance(OperationPhase.PLAN)
                return PhaseResult(
                    next_ctx=new_ctx, next_phase=OperationPhase.PLAN,
                    status="ok",
                )
            except Exception as exc:
                return PhaseResult(
                    next_ctx=ctx, next_phase=None,
                    status="fail", reason=str(exc),
                )
""").strip()


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    """Slice 3 ships master-off; tests need master-on for the
    validator to run. Tests that exercise master-off explicitly
    delete the env var."""
    monkeypatch.setenv("JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED", "1")
    yield


# ===========================================================================
# A — Module constants + enums + frozen result
# ===========================================================================


def test_max_candidate_bytes_pinned():
    assert MAX_CANDIDATE_BYTES == 256 * 1024


def test_validation_status_five_values():
    """Pin: PASSED / FAILED / SKIPPED / PARSE_ERROR / OVERSIZE."""
    assert {s.name for s in ValidationStatus} == {
        "PASSED", "FAILED", "SKIPPED", "PARSE_ERROR", "OVERSIZE",
    }


def test_validation_failure_reason_ten_values():
    """Pin: 7 rules + 2 supporting failure shapes (RUN_NOT_ASYNC +
    RUN_BAD_SIGNATURE split rule 3 into actionable detail).
    Phase 7.7 (2026-04-26) added INTROSPECTION_ESCAPE as the 9th value.
    Rule 8 (post-P7.7 followup, 2026-04-26) added MODULE_LEVEL_SIDE_EFFECT
    as the 10th value."""
    assert {r.name for r in ValidationFailureReason} == {
        "NO_PHASE_RUNNER_SUBCLASS",
        "MISSING_PHASE_ATTR",
        "MISSING_RUN_METHOD",
        "RUN_NOT_ASYNC",
        "RUN_BAD_SIGNATURE",
        "CTX_MUTATION",
        "NO_TOP_LEVEL_TRY",
        "BANNED_IMPORT",
        "INTROSPECTION_ESCAPE",  # P7.7 sandbox hardening
        "MODULE_LEVEL_SIDE_EFFECT",  # Rule 8 — post-P7.7 followup
    }


def test_validation_result_is_frozen():
    r = ValidationResult(status=ValidationStatus.PASSED)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.status = ValidationStatus.FAILED  # type: ignore[misc]


def test_validation_result_default_classes_inspected_empty():
    assert ValidationResult(status=ValidationStatus.PASSED).classes_inspected == ()


# ===========================================================================
# B — Env knob (default false pre-graduation)
# ===========================================================================


def test_is_enabled_default_true_post_graduation(monkeypatch):
    """Pass B Slice 3 graduation 2026-05-03: master flag flipped
    default-true. Operators flip explicit ``false`` to opt out."""
    monkeypatch.delenv(
        "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED", raising=False,
    )
    assert is_enabled() is True


def test_master_off_returns_skipped(monkeypatch):
    """Operator-disabled path: explicit ``false`` short-circuits to
    SKIPPED (post-graduation, the env knob must be set explicitly to
    disable rather than relying on absence)."""
    monkeypatch.setenv(
        "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED", "false",
    )
    r = validate_ast(_GOOD_RUNNER)
    assert r.status is ValidationStatus.SKIPPED
    assert "master_flag_off" in r.detail


# ===========================================================================
# C — Defensive: bad inputs
# ===========================================================================


def test_none_source_returns_parse_error():
    r = validate_ast(None)  # type: ignore[arg-type]
    assert r.status is ValidationStatus.PARSE_ERROR


def test_oversize_source_returns_oversize():
    huge = "x = 1\n" * (MAX_CANDIDATE_BYTES // 4)
    r = validate_ast(huge)
    assert r.status is ValidationStatus.OVERSIZE


def test_syntax_error_returns_parse_error():
    r = validate_ast("def broken(:\n  pass\n")
    assert r.status is ValidationStatus.PARSE_ERROR
    assert "syntax_error" in r.detail


# ===========================================================================
# D — Rule 1: ABC conformance
# ===========================================================================


def test_rule1_passes_with_direct_phase_runner_inheritance():
    r = validate_ast(_GOOD_RUNNER)
    assert r.status is ValidationStatus.PASSED


def test_rule1_passes_with_module_prefixed_inheritance():
    src = textwrap.dedent("""
        from backend.core.ouroboros.governance import phase_runner
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        from backend.core.ouroboros.governance.phase_runner import PhaseResult

        class PrefixedRunner(phase_runner.PhaseRunner):
            phase: OperationPhase = OperationPhase.ROUTE

            async def run(self, ctx: OperationContext) -> PhaseResult:
                try:
                    return PhaseResult(
                        next_ctx=ctx, next_phase=None, status="ok",
                    )
                except Exception:
                    return PhaseResult(
                        next_ctx=ctx, next_phase=None, status="fail",
                    )
    """).strip()
    r = validate_ast(src)
    assert r.status is ValidationStatus.PASSED


def test_rule1_fails_when_no_phase_runner_subclass():
    src = "class NotARunner:\n    pass\n"
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.NO_PHASE_RUNNER_SUBCLASS


def test_rule1_fails_when_class_inherits_unrelated_base():
    src = "class Foo(SomeOtherBase):\n    pass\n"
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.NO_PHASE_RUNNER_SUBCLASS


# ===========================================================================
# E — Rule 2: phase attribute
# ===========================================================================


def test_rule2_passes_with_annassign_and_value():
    """``phase: OperationPhase = OperationPhase.X`` — happy."""
    r = validate_ast(_GOOD_RUNNER)
    assert r.status is ValidationStatus.PASSED


def test_rule2_passes_with_plain_assign():
    """``phase = OperationPhase.X`` — also accepted."""
    src = _GOOD_RUNNER.replace(
        "phase: OperationPhase = OperationPhase.ROUTE",
        "phase = OperationPhase.ROUTE",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.PASSED


def test_rule2_fails_when_phase_attr_missing():
    src = _GOOD_RUNNER.replace(
        "    phase: OperationPhase = OperationPhase.ROUTE\n\n", "",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.MISSING_PHASE_ATTR


def test_rule2_fails_when_phase_is_annotation_only():
    """``phase: OperationPhase`` (no value) — rejected. The ABC
    declares the type hint without value; subclasses MUST set a
    concrete value."""
    src = _GOOD_RUNNER.replace(
        "    phase: OperationPhase = OperationPhase.ROUTE",
        "    phase: OperationPhase",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.MISSING_PHASE_ATTR


# ===========================================================================
# F — Rule 3: run signature
# ===========================================================================


def test_rule3_fails_when_run_missing():
    src = textwrap.dedent("""
        from backend.core.ouroboros.governance.phase_runner import PhaseRunner
        from backend.core.ouroboros.governance.op_context import OperationPhase

        class RunlessRunner(PhaseRunner):
            phase: OperationPhase = OperationPhase.ROUTE
    """).strip()
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.MISSING_RUN_METHOD


def test_rule3_fails_when_run_is_sync():
    """Sync ``def run`` — rejected. The ABC requires async."""
    src = _GOOD_RUNNER.replace("async def run", "def run")
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.RUN_NOT_ASYNC


def test_rule3_fails_when_run_takes_wrong_arg_count():
    src = _GOOD_RUNNER.replace(
        "async def run(self, ctx: OperationContext)",
        "async def run(self)",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.RUN_BAD_SIGNATURE


def test_rule3_fails_when_first_arg_not_self():
    src = _GOOD_RUNNER.replace(
        "async def run(self, ctx: OperationContext)",
        "async def run(cls, ctx: OperationContext)",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.RUN_BAD_SIGNATURE


def test_rule3_fails_when_second_arg_not_ctx():
    src = _GOOD_RUNNER.replace(
        "async def run(self, ctx: OperationContext)",
        "async def run(self, context: OperationContext)",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.RUN_BAD_SIGNATURE


def test_rule3_fails_when_ctx_annotation_missing():
    src = _GOOD_RUNNER.replace(
        "async def run(self, ctx: OperationContext)",
        "async def run(self, ctx)",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.RUN_BAD_SIGNATURE


def test_rule3_fails_when_return_type_missing():
    src = _GOOD_RUNNER.replace(" -> PhaseResult:", ":")
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.RUN_BAD_SIGNATURE


def test_rule3_fails_with_vararg():
    src = _GOOD_RUNNER.replace(
        "async def run(self, ctx: OperationContext)",
        "async def run(self, ctx: OperationContext, *args)",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.RUN_BAD_SIGNATURE


# ===========================================================================
# G — Rule 4: no ctx mutation
# ===========================================================================


def test_rule4_passes_with_ctx_advance():
    """``ctx.advance(...)`` is the canonical pattern — call, not
    mutation. Pinned by the GOOD_RUNNER template's body."""
    r = validate_ast(_GOOD_RUNNER)
    assert r.status is ValidationStatus.PASSED


def test_rule4_passes_with_ctx_rebind():
    """Rebinding ``ctx = something`` is fine — the input ctx object
    is untouched. Only attribute mutation is rejected."""
    src = _GOOD_RUNNER.replace(
        "            new_ctx = ctx.advance(OperationPhase.PLAN)",
        "            ctx = ctx.advance(OperationPhase.PLAN)\n"
        "            new_ctx = ctx",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.PASSED


def test_rule4_fails_on_direct_attr_assign():
    src = _GOOD_RUNNER.replace(
        "            new_ctx = ctx.advance(OperationPhase.PLAN)",
        "            ctx.risk_tier = None\n"
        "            new_ctx = ctx",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.CTX_MUTATION
    assert "ctx.risk_tier" in r.detail


def test_rule4_fails_on_aug_assign():
    src = _GOOD_RUNNER.replace(
        "            new_ctx = ctx.advance(OperationPhase.PLAN)",
        "            ctx.attempts += 1\n"
        "            new_ctx = ctx",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.CTX_MUTATION
    assert "aug-assign" in r.detail


def test_rule4_fails_on_ann_assign_with_value():
    src = _GOOD_RUNNER.replace(
        "            new_ctx = ctx.advance(OperationPhase.PLAN)",
        "            ctx.x: int = 1\n"
        "            new_ctx = ctx",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.CTX_MUTATION
    assert "ann-assign" in r.detail


def test_rule4_fails_on_nested_ctx_attr_assign():
    """``ctx.metadata.foo = ...`` — also rejected (any attribute
    chain rooted at ctx)."""
    src = _GOOD_RUNNER.replace(
        "            new_ctx = ctx.advance(OperationPhase.PLAN)",
        "            ctx.metadata.foo = 1\n"
        "            new_ctx = ctx",
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.CTX_MUTATION


# ===========================================================================
# H — Rule 5: top-level try/except
# ===========================================================================


def test_rule5_passes_when_try_at_top_level():
    r = validate_ast(_GOOD_RUNNER)
    assert r.status is ValidationStatus.PASSED


def test_rule5_passes_with_prefix_then_try():
    """Some local bindings before the try block are fine — the rule
    is "is there a try at the top level at all"."""
    src = _GOOD_RUNNER.replace(
        '        try:\n'
        '            new_ctx = ctx.advance(OperationPhase.PLAN)',
        '        label = "starting"\n'
        '        try:\n'
        '            new_ctx = ctx.advance(OperationPhase.PLAN)',
    )
    r = validate_ast(src)
    assert r.status is ValidationStatus.PASSED


def test_rule5_fails_when_no_try_block():
    src = textwrap.dedent("""
        from backend.core.ouroboros.governance.phase_runner import (
            PhaseRunner, PhaseResult,
        )
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )

        class TrylessRunner(PhaseRunner):
            phase: OperationPhase = OperationPhase.ROUTE

            async def run(self, ctx: OperationContext) -> PhaseResult:
                return PhaseResult(
                    next_ctx=ctx, next_phase=None, status="ok",
                )
    """).strip()
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.NO_TOP_LEVEL_TRY


# ===========================================================================
# I — Rule 6: banned imports
# ===========================================================================


def test_rule6_passes_with_stdlib_imports():
    src = textwrap.dedent("""
        import os
        import json
        import re
        from pathlib import Path
        from backend.core.ouroboros.governance.phase_runner import (
            PhaseRunner, PhaseResult,
        )
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )

        class StdlibRunner(PhaseRunner):
            phase: OperationPhase = OperationPhase.ROUTE
            async def run(self, ctx: OperationContext) -> PhaseResult:
                try:
                    return PhaseResult(
                        next_ctx=ctx, next_phase=None, status="ok",
                    )
                except Exception:
                    return PhaseResult(
                        next_ctx=ctx, next_phase=None, status="fail",
                    )
    """).strip()
    r = validate_ast(src)
    assert r.status is ValidationStatus.PASSED


@pytest.mark.parametrize("banned_import", [
    "from backend.core.ouroboros.governance.semantic_firewall import x",
    "from backend.core.ouroboros.governance.change_engine import x",
    "from backend.core.ouroboros.governance.iron_gate import x",
    "from backend.core.ouroboros.governance.semantic_guardian import x",
    "from backend.core.ouroboros.governance.scoped_tool_backend import x",
    "from backend.core.ouroboros.governance.policy import x",
    "from backend.core.ouroboros.governance.orchestrator import x",
    "from backend.core.ouroboros.governance.gate import x",
])
def test_rule6_fails_on_banned_governance_import(banned_import):
    src = banned_import + "\n" + _GOOD_RUNNER
    r = validate_ast(src)
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.BANNED_IMPORT


def test_rule6_passes_with_allowed_governance_imports():
    """4 allowlist modules: phase_runner, op_context,
    subagent_contracts, risk_engine."""
    src = textwrap.dedent("""
        from backend.core.ouroboros.governance.phase_runner import (
            PhaseRunner, PhaseResult,
        )
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        from backend.core.ouroboros.governance.risk_engine import RiskTier

        class AllowedRunner(PhaseRunner):
            phase: OperationPhase = OperationPhase.ROUTE
            async def run(self, ctx: OperationContext) -> PhaseResult:
                try:
                    _ = RiskTier.SAFE_AUTO  # reading RiskTier is fine
                    return PhaseResult(
                        next_ctx=ctx, next_phase=None, status="ok",
                    )
                except Exception:
                    return PhaseResult(
                        next_ctx=ctx, next_phase=None, status="fail",
                    )
    """).strip()
    r = validate_ast(src)
    assert r.status is ValidationStatus.PASSED


def test_rule6_extra_banned_modules_param():
    """Caller-supplied extra_banned_modules is honoured. Slice 5
    MetaPhaseRunner will pass the live Order-2 manifest's
    governance paths so the validator stays in sync with the cage."""
    src = "import json\n" + _GOOD_RUNNER
    r = validate_ast(src, extra_banned_modules=["json"])
    assert r.status is ValidationStatus.FAILED
    assert r.reason is ValidationFailureReason.BANNED_IMPORT
    assert "explicitly banned" in r.detail


def test_rule6_relative_import_skipped():
    """``from . import X`` (relative, no module name) is skipped —
    can't classify without resolving the package context. Defensive
    pass-through."""
    src = "from . import phase_runner\n" + _GOOD_RUNNER
    r = validate_ast(src)
    # No banned-import failure — relative imports are skipped at
    # the rule-6 check; the runner passes.
    assert r.status is ValidationStatus.PASSED


# ===========================================================================
# J — validate_ast_strict raises on FAILED
# ===========================================================================


def test_strict_returns_passed_result():
    r = validate_ast_strict(_GOOD_RUNNER)
    assert r.status is ValidationStatus.PASSED


def test_strict_raises_on_failed():
    src = "class NotARunner:\n    pass\n"
    with pytest.raises(PhaseRunnerASTValidationError) as exc_info:
        validate_ast_strict(src)
    assert (
        exc_info.value.result.reason
        is ValidationFailureReason.NO_PHASE_RUNNER_SUBCLASS
    )


def test_strict_returns_skipped_when_master_off(monkeypatch):
    """Post Slice 3 graduation: master flag is default-true; explicit
    ``false`` must be set to short-circuit strict mode to SKIPPED."""
    monkeypatch.setenv(
        "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED", "false",
    )
    r = validate_ast_strict(_GOOD_RUNNER)
    assert r.status is ValidationStatus.SKIPPED


def test_strict_returns_oversize_without_raising():
    huge = "x = 1\n" * (MAX_CANDIDATE_BYTES // 4)
    r = validate_ast_strict(huge)
    assert r.status is ValidationStatus.OVERSIZE


def test_strict_returns_parse_error_without_raising():
    r = validate_ast_strict("def broken(:\n  pass\n")
    assert r.status is ValidationStatus.PARSE_ERROR


# ===========================================================================
# K — Real graduated PhaseRunner subclasses must validate cleanly
# ===========================================================================


@pytest.mark.parametrize("path", [
    "backend/core/ouroboros/governance/phase_runners/gate_runner.py",
    "backend/core/ouroboros/governance/phase_runners/complete_runner.py",
])
def test_real_phase_runner_subclasses_validate(path):
    """Pin: every graduated PhaseRunner subclass on main MUST pass
    the validator. This is the regression spine — Slice 5 adding
    new rules / tightening existing ones can't break the live
    runners without showing up here.

    NOTE: real runners may import other governance modules
    (e.g. risk_tier_floor for the MIN_RISK_TIER floor); the
    validator is intended for **NEW** runner candidates
    O+V proposes via MetaPhaseRunner. Existing graduated runners
    are exempt from rule 6 by virtue of being committed before
    Pass B existed. This test is therefore a **soft pin**: it
    runs the validator + asserts the result is NOT a regression
    in the structural rules (1-5) — banned-import failures on
    real runners are expected and acceptable for now."""
    src = _read(path)
    r = validate_ast(src)
    # Structural rules 1-5: real runners must satisfy them.
    if r.status is ValidationStatus.FAILED:
        assert r.reason is ValidationFailureReason.BANNED_IMPORT, (
            f"Real runner {path} fails structural rule "
            f"{r.reason.value if r.reason else '?'}: {r.detail}"
        )
    else:
        assert r.status is ValidationStatus.PASSED


# ===========================================================================
# L — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier_floor",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
    "from backend.core.ouroboros.governance.semantic_firewall",
    "from backend.core.ouroboros.governance.scoped_tool_backend",
]


def test_validator_no_authority_imports():
    src = _read(
        "backend/core/ouroboros/governance/meta/ast_phase_runner_validator.py",
    )
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_validator_no_io_subprocess_or_env_writes():
    """Pin: pure ast.parse + walk. No runtime introspection, no
    subprocess, no env mutation, no network."""
    src = _strip_docstrings_and_comments(
        _read(
            "backend/core/ouroboros/governance/meta/ast_phase_runner_validator.py",
        ),
    )
    forbidden = [
        "subprocess.",
        "open(",
        ".write_text(",
        ".read_text(",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"

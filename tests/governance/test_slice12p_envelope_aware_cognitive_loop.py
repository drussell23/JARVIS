"""
Slice 12P — Envelope Awareness + Telemetry + Reflexive Healing tests.
=====================================================================

Closes the structural contradiction surfaced by the Path A
post-Slice-12O soak (bt-2026-05-23-030130): the SWE-Bench-Pro
fixture op was rejected by the Iron Gate's exploration-first
invariant because the gate enforced 2+ exploration calls before
any patch, while the fixture's structurally-no-op nature meant
the model would never invoke exploration.

PHASE 1 — Envelope-aware exploration discipline:
  * New ``envelope_metadata.is_wiring_validation_envelope(ctx)``
    composes 3 envelope-evidence signals (swe_bench_pro=True AND
    gold_patch_empty=True AND real_benchmark=False)
  * envelope_builder.py emits the 3 signals from ProblemSpec
    metadata (no hardcoded instance_ids)
  * orchestrator.py + generate_runner.py threshold-resolution
    chain drops _min_explore to 0 ONLY when the envelope matches

PHASE 2 — Structured terminal-reason telemetry:
  * New closed ``TerminalReasonClass`` enum (6 values)
  * ``classify_terminal_reason`` substring-rule classifier
  * SessionRecorder.record_operation accepts
    ``terminal_reason_code``, classifies it, and emits
    ``terminal_reason_class`` into summary.json.operations[]

PHASE 3 — Reflexive LLM-in-the-loop healing:
  * ``reflexive_healing.format_structural_rejection_feedback()``
    composes <DEVELOPER_FEEDBACK> XML block with closed-taxonomy
    class + canonical remediation actions
  * Composes the existing orchestrator retry-feedback path at
    orchestrator.py:5253+ (CRITICAL_SYSTEM_OVERRIDE pattern)
  * Returns None for non-structural rejections (no-op for
    provider exhaustion / wall cap)
  * NEVER raises; defensive on every path

Operator binding (verbatim from Slice 12P directive):
  * No hardcoded string matching for fixture detection
  * Compose existing CandidateGenerator + retry loops
  * Respect attempt limits, CostGovernor, Slice 12O cooldown
  * Structural faults natively bubble up into summary.json
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from backend.core.ouroboros.governance.envelope_metadata import (
    EVIDENCE_KEY_GOLD_PATCH_EMPTY,
    EVIDENCE_KEY_REAL_BENCHMARK,
    EVIDENCE_KEY_SWE_BENCH_PRO,
    envelope_fixture_purpose,
    envelope_is_swe_bench_pro,
    is_wiring_validation_envelope,
)
from backend.core.ouroboros.governance.reflexive_healing import (
    format_structural_rejection_feedback,
    get_remediation_actions,
)
from backend.core.ouroboros.governance.terminal_reason import (
    TerminalReasonClass,
    classify_terminal_reason,
    is_reflexive_healing_eligible,
)


# ===============================================================
# Helpers
# ===============================================================


class _FakeCtx:
    """Minimal ctx surface for envelope-metadata tests — only
    needs ``intake_evidence_json``."""
    def __init__(self, evidence_dict=None):
        if evidence_dict is None:
            self.intake_evidence_json = ""
        else:
            self.intake_evidence_json = json.dumps(evidence_dict)


# ===============================================================
# Phase 1 — Envelope awareness
# ===============================================================


def test_phase1_fixture_envelope_recognized() -> None:
    """The 3-signal AND that defines a wiring-validation fixture."""
    ctx = _FakeCtx({
        EVIDENCE_KEY_SWE_BENCH_PRO: True,
        EVIDENCE_KEY_GOLD_PATCH_EMPTY: True,
        EVIDENCE_KEY_REAL_BENCHMARK: False,
    })
    assert is_wiring_validation_envelope(ctx) is True


def test_phase1_real_benchmark_envelope_not_fixture() -> None:
    """Real SWE-Bench-Pro problems (non-empty gold_patch) must
    NOT be classified as fixtures — they SHOULD explore."""
    ctx = _FakeCtx({
        EVIDENCE_KEY_SWE_BENCH_PRO: True,
        EVIDENCE_KEY_GOLD_PATCH_EMPTY: False,
        EVIDENCE_KEY_REAL_BENCHMARK: True,
    })
    assert is_wiring_validation_envelope(ctx) is False


def test_phase1_non_swe_envelope_not_fixture() -> None:
    """Ouroboros autonomous ops (no SWE substrate) must NOT
    be classified as fixtures."""
    ctx = _FakeCtx({"sensor": "opportunity_miner"})
    assert is_wiring_validation_envelope(ctx) is False


def test_phase1_partial_signals_not_fixture() -> None:
    """All 3 signals must AND together — missing any one
    means NOT a fixture."""
    # Missing gold_patch_empty
    assert is_wiring_validation_envelope(_FakeCtx({
        EVIDENCE_KEY_SWE_BENCH_PRO: True,
        EVIDENCE_KEY_REAL_BENCHMARK: False,
    })) is False
    # Missing real_benchmark
    assert is_wiring_validation_envelope(_FakeCtx({
        EVIDENCE_KEY_SWE_BENCH_PRO: True,
        EVIDENCE_KEY_GOLD_PATCH_EMPTY: True,
    })) is False
    # Missing swe_bench_pro
    assert is_wiring_validation_envelope(_FakeCtx({
        EVIDENCE_KEY_GOLD_PATCH_EMPTY: True,
        EVIDENCE_KEY_REAL_BENCHMARK: False,
    })) is False


def test_phase1_real_benchmark_default_is_true() -> None:
    """If metadata.real_benchmark is missing, the envelope
    builder defaults to True (assume real benchmark). The fixture
    classifier requires the explicit False to flip."""
    ctx = _FakeCtx({
        EVIDENCE_KEY_SWE_BENCH_PRO: True,
        EVIDENCE_KEY_GOLD_PATCH_EMPTY: True,
        # real_benchmark missing entirely → not a fixture
    })
    assert is_wiring_validation_envelope(ctx) is False


def test_phase1_malformed_evidence_json_safe() -> None:
    """Garbage / missing / wrong-type evidence JSON returns
    False without raising."""
    assert is_wiring_validation_envelope(_FakeCtx()) is False  # empty
    assert is_wiring_validation_envelope(None) is False
    bad_ctx = _FakeCtx()
    bad_ctx.intake_evidence_json = "garbage{not_json"
    assert is_wiring_validation_envelope(bad_ctx) is False
    bad_ctx.intake_evidence_json = '"a string not a dict"'
    assert is_wiring_validation_envelope(bad_ctx) is False
    bad_ctx.intake_evidence_json = '[1, 2, 3]'  # list not dict
    assert is_wiring_validation_envelope(bad_ctx) is False


def test_phase1_envelope_is_swe_bench_pro_helper() -> None:
    """The lighter-weight envelope_is_swe_bench_pro helper
    returns True for ANY SWE-Bench-Pro envelope (fixture or
    real benchmark)."""
    assert envelope_is_swe_bench_pro(_FakeCtx({
        EVIDENCE_KEY_SWE_BENCH_PRO: True,
    })) is True
    assert envelope_is_swe_bench_pro(_FakeCtx({
        EVIDENCE_KEY_SWE_BENCH_PRO: False,
    })) is False
    assert envelope_is_swe_bench_pro(_FakeCtx()) is False


def test_phase1_fixture_purpose_extraction() -> None:
    """envelope_fixture_purpose returns the fixture's declared
    purpose string when present."""
    ctx = _FakeCtx({
        "swe_bench_pro": True,
        "fixture_purpose": "wiring_validation",
    })
    assert envelope_fixture_purpose(ctx) == "wiring_validation"
    # Missing → None
    assert envelope_fixture_purpose(_FakeCtx({})) is None
    # Wrong type → None (defensive)
    bad = _FakeCtx({"fixture_purpose": 42})
    assert envelope_fixture_purpose(bad) is None


# ===============================================================
# Phase 1 — Envelope-builder integration (the producer side)
# ===============================================================


def test_phase1_envelope_builder_emits_fixture_signals_for_empty_gold_patch() -> None:
    """When ProblemSpec.gold_patch=="" AND
    metadata.real_benchmark=False, the builder MUST emit
    gold_patch_empty=True + real_benchmark=False."""
    from backend.core.ouroboros.governance.swe_bench_pro.envelope_builder import (
        _build_evidence,
    )
    from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
        ProblemSpec,
    )

    class _FakePrepared:
        worktree_path = "/tmp/fake-wt"
        branch_name = "swebp/fake"

    fixture = ProblemSpec(
        instance_id="fake-fixture-001",
        repo="fake/repo", base_commit="abc123", problem_statement="...",
        test_patch="diff...", gold_patch="",
        metadata={"purpose": "wiring_validation", "real_benchmark": False},
    )
    ev = _build_evidence(fixture, _FakePrepared())
    assert ev["swe_bench_pro"] is True
    assert ev["gold_patch_empty"] is True
    assert ev["real_benchmark"] is False
    assert ev["fixture_purpose"] == "wiring_validation"


def test_phase1_envelope_builder_emits_real_signals_for_non_empty_gold_patch() -> None:
    """Real benchmark problems (non-empty gold_patch) MUST emit
    gold_patch_empty=False + real_benchmark=True (default)."""
    from backend.core.ouroboros.governance.swe_bench_pro.envelope_builder import (
        _build_evidence,
    )
    from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
        ProblemSpec,
    )

    class _FakePrepared:
        worktree_path = "/tmp/fake-wt"
        branch_name = "swebp/fake"

    real = ProblemSpec(
        instance_id="real-001",
        repo="real/repo", base_commit="abc", problem_statement="...",
        test_patch="diff...", gold_patch="diff actual fix content",
        metadata={},
    )
    ev = _build_evidence(real, _FakePrepared())
    assert ev["swe_bench_pro"] is True
    assert ev["gold_patch_empty"] is False
    # Default-true when metadata.real_benchmark is missing
    assert ev["real_benchmark"] is True


def test_phase1_envelope_builder_no_hardcoded_instance_ids() -> None:
    """The envelope builder MUST NOT special-case any specific
    instance_id. The metadata signals must work for ANY fixture
    name."""
    from backend.core.ouroboros.governance.swe_bench_pro.envelope_builder import (
        _build_evidence,
    )
    from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
        ProblemSpec,
    )

    class _FakePrepared:
        worktree_path = "/tmp/fake-wt"
        branch_name = "swebp/fake"

    # Different instance_ids, same metadata → same fixture
    # classification
    for instance_id in (
        "totally-different-fixture-xyz",
        "another-name-abc-789",
        "operator-renamed-fixture",
    ):
        p = ProblemSpec(
            instance_id=instance_id,
            repo="r", base_commit="c", problem_statement="p",
            test_patch="t", gold_patch="",
            metadata={"real_benchmark": False},
        )
        ev = _build_evidence(p, _FakePrepared())
        assert ev["gold_patch_empty"] is True
        assert ev["real_benchmark"] is False


# ===============================================================
# Phase 2 — TerminalReasonClass closed taxonomy + classifier
# ===============================================================


def test_phase2_taxonomy_closed_six_values() -> None:
    """The closed enum has exactly 6 values."""
    values = {m.value for m in TerminalReasonClass}
    assert values == {
        "provider_exhaustion",
        "structural_gate_rejection",
        "cost_budget_exhausted",
        "wall_clock_cap",
        "cancelled_shutdown",
        "other",
    }


def test_phase2_classifier_provider_exhaustion() -> None:
    """Provider-class terminal reasons → PROVIDER_EXHAUSTION."""
    for code in (
        "circuit_breaker_tripped:terminal_structural",
        "circuit_breaker_tripped:terminal_quota",
        "all_providers_exhausted:circuit_breaker_tripped:terminal_structural",
        "provider_exhausted",
        "stream_rupture_mid_generate",
        "stream_disconnected",
        "stream_eof_unexpected",
        "stream_timeout_after_60s",
    ):
        assert classify_terminal_reason(code) == \
            TerminalReasonClass.PROVIDER_EXHAUSTION, code


def test_phase2_classifier_structural_gate_rejection() -> None:
    """Structural-gate reasons → STRUCTURAL_GATE_REJECTION."""
    for code in (
        "exploration_insufficient: 0/2 exploration tool calls",
        "ascii_gate_failed",
        "semantic_guard_credential_introduced",
        "semantic_guard_test_assertion_inverted",
        "adversarial_reviewer_rejected",
        "iron_gate_blast_radius_exceeded",
    ):
        assert classify_terminal_reason(code) == \
            TerminalReasonClass.STRUCTURAL_GATE_REJECTION, code


def test_phase2_classifier_cost_budget_exhausted() -> None:
    """Budget-cap reasons → COST_BUDGET_EXHAUSTED."""
    for code in (
        "budget_floor_breached",
        "cost_cap_reached",
        "budget_exhausted",
    ):
        assert classify_terminal_reason(code) == \
            TerminalReasonClass.COST_BUDGET_EXHAUSTED, code


def test_phase2_classifier_wall_clock_cap() -> None:
    """WallClockWatchdog cause → WALL_CLOCK_CAP."""
    assert classify_terminal_reason("wall_clock_cap") == \
        TerminalReasonClass.WALL_CLOCK_CAP


def test_phase2_classifier_cancelled_shutdown_priority() -> None:
    """``cooldown_cancelled_shutdown`` MUST classify as
    CANCELLED_SHUTDOWN, NOT as PROVIDER_EXHAUSTION (even though
    the underlying cause that triggered the cooldown was
    provider exhaustion). The shutdown signal is structurally
    cleaner."""
    for code in (
        "cooldown_cancelled_shutdown",
        "session_exhausted_shutdown",
        "cancelled_during_shutdown",
    ):
        assert classify_terminal_reason(code) == \
            TerminalReasonClass.CANCELLED_SHUTDOWN, code


def test_phase2_classifier_other_for_unknown() -> None:
    """Unknown / empty / non-string → OTHER."""
    for code in ("", "totally_unrecognized_xyz", None, 42, [], {}):
        assert classify_terminal_reason(code) == \
            TerminalReasonClass.OTHER


def test_phase2_reflexive_healing_eligible_gates() -> None:
    """Reflexive healing applies ONLY to structural gate
    rejections — provider exhaustion is Slice 12O territory,
    shutdown/wall/budget terminate cleanly."""
    assert is_reflexive_healing_eligible("exploration_insufficient: 0/2") is True
    assert is_reflexive_healing_eligible("ascii_gate_failed") is True
    assert is_reflexive_healing_eligible("circuit_breaker_tripped:terminal_structural") is False
    assert is_reflexive_healing_eligible("wall_clock_cap") is False
    assert is_reflexive_healing_eligible("cooldown_cancelled_shutdown") is False
    assert is_reflexive_healing_eligible("budget_floor_breached") is False
    assert is_reflexive_healing_eligible(None) is False
    assert is_reflexive_healing_eligible("") is False


def test_phase2_session_recorder_records_terminal_reason_class() -> None:
    """SessionRecorder.record_operation MUST persist
    terminal_reason_code + terminal_reason_class into the
    operations[] entry."""
    from backend.core.ouroboros.battle_test.session_recorder import (
        SessionRecorder,
    )
    rec = SessionRecorder(session_id="test")
    rec.record_operation(
        op_id="op-x", status="failed",
        sensor="test", technique="test",
        composite_score=0.0, elapsed_s=0.0,
        terminal_reason_code="exploration_insufficient: 0/2",
    )
    op = rec._operations[-1]
    assert op["terminal_reason_code"] == "exploration_insufficient: 0/2"
    assert op["terminal_reason_class"] == "structural_gate_rejection"


def test_phase2_session_recorder_backward_compat_default() -> None:
    """Pre-Slice-12P callers that don't pass terminal_reason_code
    still work — defaults to empty + OTHER."""
    from backend.core.ouroboros.battle_test.session_recorder import (
        SessionRecorder,
    )
    rec = SessionRecorder(session_id="test")
    rec.record_operation(
        op_id="legacy-op", status="completed",
        sensor="test", technique="test",
        composite_score=0.0, elapsed_s=1.0,
    )
    op = rec._operations[-1]
    assert op["terminal_reason_code"] == ""
    assert op["terminal_reason_class"] == "other"


# ===============================================================
# Phase 3 — Reflexive healing formatter
# ===============================================================


def test_phase3_formatter_emits_developer_feedback_block() -> None:
    """The formatter wraps the rejection in <DEVELOPER_FEEDBACK>
    XML with priority=CRITICAL_SYSTEM_OVERRIDE so the model's
    attention mechanism gives it priority over front-loaded
    task text."""
    block = format_structural_rejection_feedback(
        "exploration_insufficient: 0/2",
        rejection_detail="0/2 exploration tool calls",
    )
    assert block is not None
    assert "<DEVELOPER_FEEDBACK" in block
    assert 'priority="CRITICAL_SYSTEM_OVERRIDE"' in block
    assert "</DEVELOPER_FEEDBACK>" in block
    assert "REQUIRED ACTIONS" in block


def test_phase3_formatter_includes_remediation_action_list() -> None:
    """The block includes the canonical remediation actions
    for the matched rejection class."""
    block = format_structural_rejection_feedback(
        "exploration_insufficient: 0/2 exploration tool calls",
    )
    assert block is not None
    assert "read_file" in block
    assert "search_code" in block.lower() or "get_callers" in block.lower()
    assert "Do NOT" in block  # explicit negative directive present


def test_phase3_formatter_returns_none_for_non_structural() -> None:
    """Provider exhaustion / wall cap / shutdown / budget / OTHER
    all return None — formatter is structural-rejection-only."""
    for code in (
        "circuit_breaker_tripped:terminal_structural",
        "wall_clock_cap",
        "cooldown_cancelled_shutdown",
        "budget_floor_breached",
        "totally_unknown_xyz",
        "",
    ):
        assert format_structural_rejection_feedback(code) is None, code


def test_phase3_formatter_includes_attempt_attribution() -> None:
    """When attempt_number + max_attempts are supplied, the block
    surfaces them so the model sees its retry context."""
    block = format_structural_rejection_feedback(
        "exploration_insufficient: ...",
        attempt_number=2, max_attempts=3,
    )
    assert block is not None
    assert "attempt=2/3" in block


def test_phase3_get_remediation_actions_returns_tuple() -> None:
    """get_remediation_actions returns the canonical action
    tuple for the matched rejection."""
    actions = get_remediation_actions("exploration_insufficient: 0/2")
    assert actions is not None
    assert len(actions) >= 3
    assert all(isinstance(a, str) for a in actions)
    assert all(len(a) > 0 for a in actions)


def test_phase3_get_remediation_actions_handles_all_structural_classes() -> None:
    """Every STRUCTURAL_GATE_REJECTION substring rule from
    terminal_reason.py MUST have a corresponding remediation
    entry — no orphaned classes."""
    for code in (
        "exploration_insufficient: ...",
        "ascii_gate_failed",
        "semantic_guard_credential_introduced",
        "iron_gate_invariant_violated",
        "adversarial_reviewer_rejected",
    ):
        actions = get_remediation_actions(code)
        assert actions is not None, code
        assert len(actions) > 0


def test_phase3_formatter_never_raises() -> None:
    """NEVER-raise contract."""
    for bad in (None, 42, b"bytes", [], {}, object()):
        # Should not raise
        try:
            _ = format_structural_rejection_feedback(bad)  # type: ignore[arg-type]
        except Exception as e:
            pytest.fail(f"raised on {bad!r}: {e}")


# ===============================================================
# Integration — orchestrator threshold-chain composition
# ===============================================================


def test_integration_orchestrator_threshold_chain_calls_slice12p_helper() -> None:
    """The orchestrator's Iron Gate threshold-resolution chain
    MUST call ``is_wiring_validation_envelope`` so envelope
    metadata can override the floor."""
    orchestrator_src = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "orchestrator.py"
    ).read_text()
    assert "is_wiring_validation_envelope" in orchestrator_src
    assert "envelope_metadata" in orchestrator_src
    assert "Slice 12P" in orchestrator_src


def test_integration_generate_runner_threshold_chain_calls_slice12p_helper() -> None:
    """Same wiring required in the paired generate_runner.py."""
    src = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "phase_runners" / "generate_runner.py"
    ).read_text()
    assert "is_wiring_validation_envelope" in src
    assert "envelope_metadata" in src


def test_integration_orchestrator_calls_reflexive_healing_formatter() -> None:
    """The orchestrator's retry-feedback site MUST compose the
    Slice 12P reflexive healing formatter so structural
    rejections get the structured DEVELOPER_FEEDBACK prepend."""
    orchestrator_src = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "orchestrator.py"
    ).read_text()
    assert "format_structural_rejection_feedback" in orchestrator_src
    assert "reflexive_healing" in orchestrator_src


# ===============================================================
# AST pins — structural regression armor
# ===============================================================


def _load_ast(rel_path: str) -> ast.Module:
    p = Path(__file__).resolve().parents[2] / rel_path
    return ast.parse(p.read_text())


def test_ast_pin_terminal_reason_class_taxonomy_closed() -> None:
    """The 6 TerminalReasonClass values are the closed taxonomy.
    Adding a new value silently could leave summary.json
    consumers with unknown enum strings."""
    tree = _load_ast(
        "backend/core/ouroboros/governance/terminal_reason.py"
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "TerminalReasonClass":
            continue
        values = set()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                values.add(stmt.targets[0].id)
        assert values == {
            "PROVIDER_EXHAUSTION", "STRUCTURAL_GATE_REJECTION",
            "COST_BUDGET_EXHAUSTED", "WALL_CLOCK_CAP",
            "CANCELLED_SHUTDOWN", "OTHER",
        }
        return
    pytest.fail("TerminalReasonClass class not found")


def test_ast_pin_envelope_metadata_evidence_keys_present() -> None:
    """The 4 evidence-key constants MUST be present at module
    level so producer (envelope_builder) and consumer
    (envelope_metadata) can't drift."""
    src = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "envelope_metadata.py"
    ).read_text()
    for key_const in (
        "EVIDENCE_KEY_SWE_BENCH_PRO",
        "EVIDENCE_KEY_GOLD_PATCH_EMPTY",
        "EVIDENCE_KEY_REAL_BENCHMARK",
        "EVIDENCE_KEY_FIXTURE_PURPOSE",
    ):
        assert key_const in src


def test_ast_pin_envelope_metadata_no_hardcoded_instance_ids() -> None:
    """``envelope_metadata.py`` code (excluding module docstring +
    triple-quoted comments) MUST NOT reference any specific
    SWE-Bench instance_id — the entire point is metadata-driven
    classification.

    AST-walks the module looking for string literals OR
    Constant.value strings that contain forbidden hardcoding
    tokens. Comments + the module docstring are excluded (the
    module docstring is allowed to MENTION the empirical-context
    fixture name for traceability)."""
    tree = _load_ast(
        "backend/core/ouroboros/governance/envelope_metadata.py"
    )
    # Drop the module-level docstring if present
    if (tree.body and isinstance(tree.body[0], ast.Expr) and
            isinstance(tree.body[0].value, ast.Constant) and
            isinstance(tree.body[0].value.value, str)):
        body = tree.body[1:]
    else:
        body = list(tree.body)
    # Build a sub-tree without the docstring + walk
    for node in body:
        for sub in ast.walk(node):
            # Skip nested docstrings (FunctionDef/ClassDef first
            # statement that's an Expr-of-Constant-str)
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
                if sub.body and isinstance(sub.body[0], ast.Expr) and \
                        isinstance(sub.body[0].value, ast.Constant) and \
                        isinstance(sub.body[0].value.value, str):
                    # Replace docstring with pass for the walk
                    sub.body[0] = ast.Pass()
            if isinstance(sub, ast.Constant) and \
                    isinstance(sub.value, str):
                for forbidden in (
                    "jarvis__harness-smoke",
                    "octocat",
                ):
                    assert forbidden not in sub.value, (
                        f"Hardcoded {forbidden!r} found at "
                        f"line {sub.lineno} (in non-docstring code)"
                    )


def test_ast_pin_envelope_builder_emits_phase1_keys() -> None:
    """envelope_builder._build_evidence MUST emit the 4 Slice 12P
    keys so the consumer can read them."""
    src = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "swe_bench_pro" / "envelope_builder.py"
    ).read_text()
    for key in (
        '"swe_bench_pro"',
        '"gold_patch_empty"',
        '"real_benchmark"',
        '"fixture_purpose"',
    ):
        assert key in src, f"envelope_builder must emit {key}"


def test_ast_pin_reflexive_healing_returns_none_for_non_structural() -> None:
    """``format_structural_rejection_feedback`` MUST gate on
    ``is_reflexive_healing_eligible`` as its FIRST check so
    non-structural rejections short-circuit without composing
    any wrapper text."""
    tree = _load_ast(
        "backend/core/ouroboros/governance/reflexive_healing.py"
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "format_structural_rejection_feedback":
            continue
        src = ast.unparse(node)
        assert "is_reflexive_healing_eligible" in src
        # Early-return on non-eligible
        assert "return None" in src
        return
    pytest.fail("format_structural_rejection_feedback not found")


def test_ast_pin_session_recorder_emits_terminal_reason_class() -> None:
    """SessionRecorder.record_operation MUST emit
    terminal_reason_class into the entry dict."""
    src = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "battle_test"
        / "session_recorder.py"
    ).read_text()
    assert "terminal_reason_class" in src
    assert "classify_terminal_reason" in src


def test_ast_pin_orchestrator_uses_envelope_aware_threshold_override() -> None:
    """orchestrator.py MUST set _min_explore = 0 (or equivalent)
    when is_wiring_validation_envelope returns True. This is the
    load-bearing Phase 1 wiring."""
    src = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "orchestrator.py"
    ).read_text()
    # The Slice 12P override block must contain both the check
    # and the floor-zero assignment
    assert "_slice12p_is_fixture" in src
    assert "_min_explore = 0" in src


def test_ast_pin_no_blocking_calls_in_envelope_metadata() -> None:
    """The envelope metadata module is on the orchestrator hot
    path — no time.sleep, no subprocess.run, no sync I/O calls."""
    tree = _load_ast(
        "backend/core/ouroboros/governance/envelope_metadata.py"
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in ("sleep", "run", "Popen", "check_output"):
                    if isinstance(node.func.value, ast.Name) and \
                            node.func.value.id in ("time", "subprocess"):
                        pytest.fail(
                            f"Blocking call {node.func.value.id}."
                            f"{node.func.attr} at line {node.lineno}"
                        )

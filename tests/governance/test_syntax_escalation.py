"""Tests for the SyntaxExhaustionEscalator — DW → J-Prime cascade engine.

Validates:
- Recording and threshold detection
- Anti-hallucination directive generation
- Context enrichment for J-Prime
- TTL-based stale entry reaping
- Master switch gating
- Double-escalation prevention
"""
from __future__ import annotations

import dataclasses
import os
import time
from unittest import mock

import pytest

from backend.core.ouroboros.governance.syntax_escalation import (
    EscalationContext,
    SyntaxExhaustionEscalator,
    SyntaxFailureRecord,
    _build_anti_hallucination_directive,
    _escalation_enabled,
    _escalation_threshold,
    _escalation_ttl_s,
    _is_syntax_class_failure,
    enrich_context_for_escalation,
    get_escalator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class _FakeContext:
    """Minimal stand-in for OperationContext."""
    op_id: str = "op-test-001"
    description: str = "Fix the failing test"
    target_files: tuple = ("backend/leaf_predicates.py",)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

class TestErrorClassification:
    def test_syntax_error_detected(self):
        assert _is_syntax_class_failure(
            "doubleword_schema_invalid:all_candidates_syntax_error"
        )

    def test_uppercase_detected(self):
        assert _is_syntax_class_failure(
            "ALL_CANDIDATES_SYNTAX_ERROR"
        )

    def test_non_syntax_error_rejected(self):
        assert not _is_syntax_class_failure("timeout:connection_refused")

    def test_empty_string_rejected(self):
        assert not _is_syntax_class_failure("")

    def test_none_rejected(self):
        assert not _is_syntax_class_failure(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Recording + threshold
# ---------------------------------------------------------------------------

class TestEscalatorRecording:
    def test_record_increments_count(self):
        esc = SyntaxExhaustionEscalator()
        esc.record_attempt("op-1", "all_candidates_syntax_error", "preview", "file.py")
        assert esc.get_failure_count("op-1") == 1

    def test_non_syntax_error_not_recorded(self):
        esc = SyntaxExhaustionEscalator()
        esc.record_attempt("op-1", "timeout:connection_refused")
        assert esc.get_failure_count("op-1") == 0

    def test_threshold_default_2(self):
        esc = SyntaxExhaustionEscalator()
        esc.record_attempt("op-1", "all_candidates_syntax_error")
        assert not esc.should_escalate("op-1")
        esc.record_attempt("op-1", "all_candidates_syntax_error")
        assert esc.should_escalate("op-1")

    def test_threshold_env_override(self):
        esc = SyntaxExhaustionEscalator()
        with mock.patch.dict(os.environ, {"JARVIS_SYNTAX_ESCALATION_THRESHOLD": "5"}):
            for _ in range(4):
                esc.record_attempt("op-1", "all_candidates_syntax_error")
            assert not esc.should_escalate("op-1")
            esc.record_attempt("op-1", "all_candidates_syntax_error")
            assert esc.should_escalate("op-1")

    def test_clear_resets_state(self):
        esc = SyntaxExhaustionEscalator()
        esc.record_attempt("op-1", "all_candidates_syntax_error")
        esc.record_attempt("op-1", "all_candidates_syntax_error")
        assert esc.should_escalate("op-1")
        esc.clear("op-1")
        assert not esc.should_escalate("op-1")
        assert esc.get_failure_count("op-1") == 0

    def test_per_op_isolation(self):
        esc = SyntaxExhaustionEscalator()
        esc.record_attempt("op-1", "all_candidates_syntax_error")
        esc.record_attempt("op-1", "all_candidates_syntax_error")
        esc.record_attempt("op-2", "all_candidates_syntax_error")
        assert esc.should_escalate("op-1")
        assert not esc.should_escalate("op-2")


# ---------------------------------------------------------------------------
# Double-escalation prevention
# ---------------------------------------------------------------------------

class TestDoubleEscalation:
    def test_mark_escalated_prevents_second(self):
        esc = SyntaxExhaustionEscalator()
        esc.record_attempt("op-1", "all_candidates_syntax_error")
        esc.record_attempt("op-1", "all_candidates_syntax_error")
        assert esc.should_escalate("op-1")
        esc.mark_escalated("op-1")
        assert not esc.should_escalate("op-1")


# ---------------------------------------------------------------------------
# Master switch
# ---------------------------------------------------------------------------

class TestMasterSwitch:
    def test_disabled_prevents_recording(self):
        esc = SyntaxExhaustionEscalator()
        with mock.patch.dict(os.environ, {"JARVIS_SYNTAX_ESCALATION_ENABLED": "false"}):
            esc.record_attempt("op-1", "all_candidates_syntax_error")
            assert esc.get_failure_count("op-1") == 0

    def test_disabled_prevents_escalation(self):
        esc = SyntaxExhaustionEscalator()
        esc.record_attempt("op-1", "all_candidates_syntax_error")
        esc.record_attempt("op-1", "all_candidates_syntax_error")
        with mock.patch.dict(os.environ, {"JARVIS_SYNTAX_ESCALATION_ENABLED": "false"}):
            assert not esc.should_escalate("op-1")


# ---------------------------------------------------------------------------
# TTL reaping
# ---------------------------------------------------------------------------

class TestTTLReaping:
    def test_stale_entries_reaped(self):
        esc = SyntaxExhaustionEscalator()
        esc.record_attempt("op-old", "all_candidates_syntax_error")
        # Manipulate timestamp to simulate old entry
        esc._ops["op-old"].last_activity = time.monotonic() - 700
        # Next record triggers reaping
        esc.record_attempt("op-new", "all_candidates_syntax_error")
        assert esc.get_failure_count("op-old") == 0
        assert esc.get_failure_count("op-new") == 1


# ---------------------------------------------------------------------------
# Escalation context building
# ---------------------------------------------------------------------------

class TestEscalationContext:
    def test_build_context_with_failures(self):
        esc = SyntaxExhaustionEscalator()
        esc.record_attempt(
            "op-1", "all_candidates_syntax_error",
            candidate_preview='{"candidates": [{"full_content": "def foo()"}]}',
            target_file="leaf_predicates.py",
        )
        esc.record_attempt(
            "op-1", "all_candidates_syntax_error",
            candidate_preview='{"candidates": [{"full_content": "def bar():"}]}',
            target_file="leaf_predicates.py",
        )
        ctx = esc.build_escalation_context("op-1", _FakeContext())
        assert ctx.consecutive_failures == 2
        assert ctx.target_file == "leaf_predicates.py"
        assert "PROVIDER ESCALATION" in ctx.anti_hallucination_directive
        assert "2 generation" in ctx.anti_hallucination_directive
        assert len(ctx.failure_records) == 2

    def test_build_context_empty_op(self):
        esc = SyntaxExhaustionEscalator()
        ctx = esc.build_escalation_context("op-nonexistent")
        assert ctx.consecutive_failures == 0
        assert ctx.anti_hallucination_directive == ""

    def test_target_file_from_context_fallback(self):
        esc = SyntaxExhaustionEscalator()
        esc.record_attempt("op-1", "all_candidates_syntax_error")
        ctx = esc.build_escalation_context(
            "op-1",
            _FakeContext(target_files=("my_file.py",)),
        )
        assert ctx.target_file == "my_file.py"


# ---------------------------------------------------------------------------
# Anti-hallucination directive
# ---------------------------------------------------------------------------

class TestAntiHallucinationDirective:
    def test_empty_failures(self):
        assert _build_anti_hallucination_directive([], "file.py") == ""

    def test_directive_includes_constraints(self):
        failures = [
            SyntaxFailureRecord(
                timestamp=0.0,
                error_msg="syntax_error",
                candidate_preview="def broken():\n  return",
                target_file="file.py",
            ),
        ]
        directive = _build_anti_hallucination_directive(failures, "file.py")
        assert "ast.parse()" in directive
        assert "COMPLETE file content" in directive
        assert "Do NOT truncate" in directive

    def test_directive_includes_previews(self):
        failures = [
            SyntaxFailureRecord(0.0, "err", "preview_content_1", "f.py"),
            SyntaxFailureRecord(0.0, "err", "preview_content_2", "f.py"),
        ]
        directive = _build_anti_hallucination_directive(failures, "f.py")
        assert "preview_content_1" in directive
        assert "preview_content_2" in directive


# ---------------------------------------------------------------------------
# Context enrichment
# ---------------------------------------------------------------------------

class TestContextEnrichment:
    def test_enrich_prepends_directive(self):
        ctx = _FakeContext(description="Original task description")
        escalation = EscalationContext(
            op_id="op-1",
            consecutive_failures=3,
            target_file="file.py",
            failure_records=(),
            anti_hallucination_directive="TEST DIRECTIVE",
        )
        enriched = enrich_context_for_escalation(ctx, escalation)
        assert enriched.description.startswith("TEST DIRECTIVE")
        assert "Original task description" in enriched.description

    def test_enrich_failsoft_on_non_dataclass(self):
        """Non-dataclass context returns unchanged."""
        result = enrich_context_for_escalation(
            {"not": "a dataclass"},
            EscalationContext("op", 0, "", (), ""),
        )
        assert result == {"not": "a dataclass"}


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

class TestEnvHelpers:
    def test_enabled_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_SYNTAX_ESCALATION_ENABLED", None)
            assert _escalation_enabled() is True

    def test_threshold_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_SYNTAX_ESCALATION_THRESHOLD", None)
            assert _escalation_threshold() == 2

    def test_threshold_minimum_1(self):
        with mock.patch.dict(os.environ, {"JARVIS_SYNTAX_ESCALATION_THRESHOLD": "0"}):
            assert _escalation_threshold() == 1

    def test_ttl_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_SYNTAX_ESCALATION_TTL_S", None)
            assert _escalation_ttl_s() == 600.0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_get_escalator_returns_same_instance(self):
        # Reset singleton for clean test
        import backend.core.ouroboros.governance.syntax_escalation as mod
        mod._ESCALATOR = None
        e1 = get_escalator()
        e2 = get_escalator()
        assert e1 is e2
        mod._ESCALATOR = None  # cleanup

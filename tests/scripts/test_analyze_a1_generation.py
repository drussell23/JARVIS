"""Tests for the A1 generation diagnostic extractor's classifiers."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_SCRIPTS_DIR = str((Path(__file__).parent.parent.parent / "scripts").resolve())
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS_DIR, name + ".py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_a = _load("analyze_a1_generation")


# --- mutation fixability ---

def test_binop_flip_is_trivially_fixable():
    m = _a.classify_mutation_fixability(
        {"mutation_kind": "binop:Div->Mult",
         "mutation_detail": {"original_segment": "/", "mutated_segment": "*"}}
    )
    assert m["trivially_fixable"] is True
    assert m["verdict"] == "trivially_fixable"


def test_body_deletion_is_ast_fatal():
    m = _a.classify_mutation_fixability(
        {"mutation_kind": "stmt:delete_body",
         "mutation_detail": {"original_segment": "return compute(x)/y", "mutated_segment": ""}}
    )
    assert m["trivially_fixable"] is False
    assert m["verdict"] == "ast_fatal_or_complex"


# --- op failure classification ---

def test_provider_exhaustion_when_no_candidate():
    op = {"terminal_state": "failed", "candidate_generated": False,
          "exhaustion_events": ["fallback_skipped:no_fallback_configured"],
          "approval_required": False, "validate_failed": False}
    assert _a.classify_op_failure(op, mutation_fixable=True) == "PROVIDER_EXHAUSTION"


def test_validation_failure_when_candidate_but_tests_fail():
    op = {"terminal_state": "failed", "candidate_generated": True,
          "exhaustion_events": [], "approval_required": False, "validate_failed": True}
    assert _a.classify_op_failure(op, mutation_fixable=True) == "VALIDATION_FAILURE"


def test_orange_block_correct_when_mutation_fatal():
    op = {"terminal_state": "blocked", "candidate_generated": False,
          "exhaustion_events": [], "approval_required": True, "validate_failed": False}
    assert _a.classify_op_failure(op, mutation_fixable=False) == "ORANGE_BLOCK_CORRECT"


def test_risk_misclassification_when_blocked_but_trivially_fixable():
    op = {"terminal_state": "blocked", "candidate_generated": False,
          "exhaustion_events": [], "approval_required": True, "validate_failed": False}
    assert _a.classify_op_failure(op, mutation_fixable=True) == "RISK_MISCLASSIFICATION"


def test_applied_is_ok():
    op = {"terminal_state": "applied", "candidate_generated": True,
          "exhaustion_events": [], "approval_required": False, "validate_failed": False}
    assert _a.classify_op_failure(op, mutation_fixable=True) == "APPLIED_OK"


# --- log extraction (synthetic lines mirroring the real debug.log) ---

def test_extract_ops_from_log_lines():
    lines = [
        "[A1Trace] accept goal=op-019fAAAA-bbbb-7f1e-cau phase=CLASSIFY",
        "[Advisor] caution (risk=0.30, blast=0) op=op-019fAAAA-bbbb-7f1e-cau",
        "[CandidateGenerator] Sentinel dispatch: attempting model=Qwen/Qwen3.5-397B-A17B-FP8 op=op-019fAAAA-bbbb-7f1e-cau",
        "[CandidateGenerator] EXHAUSTION event_n=1 cause=fallback_skipped:no_fallback_configured op=op-019fAAAA-bbbb-7f1e-cau",
        "[Slice74Probe] LEDGER_TERMINAL op_id=op-019fAAAA-bbbb-7f1e-cau state=failed written=False",
    ]
    ops = _a.extract_ops(lines)
    rec = ops["op-019fAAAA-bbbb"]
    assert rec["terminal_state"] == "failed"
    assert rec["risk"] == 0.30
    assert "Qwen/Qwen3.5-397B-A17B-FP8" in rec["models_attempted"]
    assert rec["exhaustion_events"] == ["fallback_skipped:no_fallback_configured"]
    assert rec["candidate_generated"] is False

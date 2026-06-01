"""Slice 59 — complexity-classifier calibration for non-concrete targets.

v57 (bt-2026-06-01-231338): a proactive-exploration op with a DIRECTORY target
(`target_files=['backend/core/']`) and a vague goal was classified trivial/simple
(file_count=1) → reasoning_effort=none + fast_path → DW emitted a 200-byte
fragment for a 10,912-byte file (correctly skipped by the providers.py:4507
completeness guard, so 0 commits). Root cause: the LIVE classifier
(complexity_classifier.OperationComplexityClassifier, used by classify_runner)
treats a directory target as a single concrete file.

Fix: a text-only "concrete file target" check. A non-concrete (directory-level)
target can never be TRIVIAL/SIMPLE — escalate so the op gets full reasoning
headroom (or is gated upstream by the Advisor blast-radius check, which already
blocks the high-blast directory ops). Architectural keywords and the
benchmark source-floor still take precedence; empty target_files (SWE-bench
no-target ops) are untouched.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.complexity_classifier import (
    OperationComplexityClassifier,
    ComplexityClass,
    _is_concrete_file_target,
)

_FAST = {ComplexityClass.TRIVIAL, ComplexityClass.SIMPLE}


def _clf() -> OperationComplexityClassifier:
    return OperationComplexityClassifier()


def test_is_concrete_file_target():
    assert _is_concrete_file_target("tests/foo.py") is True
    assert _is_concrete_file_target("requirements.txt") is True
    assert _is_concrete_file_target(".gitignore") is True          # dotfile = real file
    assert _is_concrete_file_target("backend/core/") is False       # trailing slash = dir
    assert _is_concrete_file_target("backend/core") is False        # no extension = dir-like
    assert _is_concrete_file_target("") is False
    assert _is_concrete_file_target("   ") is False


def test_directory_target_never_fast_path():
    r = _clf().classify(description="fix the import error", target_files=["backend/core/"])
    assert r.complexity not in _FAST, f"directory target classified {r.complexity}"


def test_v57_vague_proactive_dir_op_escalated():
    # The exact v57 op that produced the truncated fragment.
    r = _clf().classify(
        description=("Proactive exploration: domain code_gen::.py has persistent "
                     "uncertainty; address import_error proactively in your solution."),
        target_files=["backend/core/"],
        source="exploration",
    )
    assert r.complexity not in _FAST


def test_concrete_file_can_still_be_fast_path():
    # A specific file + trivial intent must STILL be allowed to fast-path
    # (zero regression for the change-producing ops like v48's todo target).
    r = _clf().classify(
        description="fix typo in comment",
        target_files=["tests/test_ouroboros_governance/test_todo_scanner_trigger_tag.py"],
    )
    assert r.complexity in (ComplexityClass.TRIVIAL, ComplexityClass.SIMPLE,
                            ComplexityClass.MODERATE)


def test_architectural_keyword_still_wins_over_dir_guard():
    r = _clf().classify(description="redesign the system architecture",
                        target_files=["backend/core/"])
    assert r.complexity == ComplexityClass.ARCHITECTURAL


def test_no_target_files_not_escalated_by_dir_guard():
    # SWE-bench-style no-target op: the dir guard must NOT fire on empty
    # target_files (the source-floor owns those). file_count=0 -> SIMPLE.
    r = _clf().classify(description="resolve the failing test", target_files=[])
    assert r.complexity == ComplexityClass.SIMPLE

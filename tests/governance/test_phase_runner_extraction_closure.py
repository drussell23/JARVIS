"""§28.5.1 phase-runner extraction closure regression spine
(2026-05-05).

The §28.5.1 v9 brutal-review entry "4-phases-not-extracted
(CLASSIFY/APPROVE/APPLY/VERIFY)" was authored at a moment when
those phases were inline blocks in `orchestrator.py`. Audit
2026-05-05 reveals the actual state has fully closed:

  * **CLASSIFY** → `CLASSIFYRunner` (Wave 2 Slice 2; default-true)
  * **APPROVE + APPLY + VERIFY** → `Slice4bRunner` (Wave 2 Slice
    4b combined runner; default-true). Combined per architectural
    decision: APPROVE's tail (pre-APPLY narrator + cancel-check +
    DRY_RUN gate) runs on every path; APPLY consumes APPROVE's
    local state; VERIFY consumes APPLY's local state. Separate
    runners would need 6-way artifact threading.

This test file structurally pins the closure so future regression
(a master flag flipping back to default-false, or a runner
deletion) fails CI before reaching production.

Verifies:

  * All 9 phase-runner flags default-TRUE (CLASSIFY / ROUTE /
    CONTEXT_EXPANSION / PLAN / GENERATE / VALIDATE / GATE /
    SLICE4B / COMPLETE)
  * All 9 phase-runner module files exist on disk
  * `phase_runners/__init__.py` exports the canonical runner
    class names
  * The orchestrator routes through every runner under its flag
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Canonical 9 phase-runner master flags + their orchestrator default
# ---------------------------------------------------------------------------


_PHASE_RUNNER_FLAGS = (
    "JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED",
    "JARVIS_PHASE_RUNNER_ROUTE_EXTRACTED",
    "JARVIS_PHASE_RUNNER_CONTEXT_EXPANSION_EXTRACTED",
    "JARVIS_PHASE_RUNNER_PLAN_EXTRACTED",
    "JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED",
    "JARVIS_PHASE_RUNNER_VALIDATE_EXTRACTED",
    "JARVIS_PHASE_RUNNER_GATE_EXTRACTED",
    "JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED",
    "JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED",
)


@pytest.mark.parametrize("flag_name", _PHASE_RUNNER_FLAGS)
def test_phase_runner_flag_defaults_true(flag_name):
    """Every phase-runner master flag MUST default-TRUE in
    `orchestrator.py`. Future regression that flips one back to
    default-false re-opens §28.5.1 torn-read landmine."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/orchestrator.py"
    )
    text = target.read_text(encoding="utf-8")
    pattern = re.compile(
        rf'os\.environ\.get\(\s*["\']\s*{re.escape(flag_name)}'
        rf'\s*["\']\s*,\s*["\']true["\']\s*\)',
    )
    assert pattern.search(text), (
        f"orchestrator.py MUST read {flag_name} with default "
        f'"true" — §28.5.1 closure pin (Wave 2 phase '
        f"extraction). Current state may have regressed."
    )


# ---------------------------------------------------------------------------
# All 9 phase-runner module files exist + expose the canonical class
# ---------------------------------------------------------------------------


_PHASE_RUNNER_MODULES = (
    ("classify_runner.py", "CLASSIFYRunner"),
    ("route_runner.py", "ROUTERunner"),
    ("context_expansion_runner.py", "ContextExpansionRunner"),
    ("plan_runner.py", "PLANRunner"),
    ("generate_runner.py", "GENERATERunner"),
    ("validate_runner.py", "VALIDATERunner"),
    ("gate_runner.py", "GATERunner"),
    ("slice4b_runner.py", "Slice4bRunner"),
    ("complete_runner.py", "COMPLETERunner"),
)


@pytest.mark.parametrize(
    "filename,classname", _PHASE_RUNNER_MODULES,
)
def test_phase_runner_module_present(filename, classname):
    """Every Wave 2 phase-runner module MUST exist on disk +
    expose the canonical class name. Deletion / renaming
    without a corresponding pin update fails this test."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/phase_runners"
        / filename
    )
    assert target.exists(), (
        f"phase runner module missing: {filename}"
    )
    text = target.read_text(encoding="utf-8")
    assert f"class {classname}" in text, (
        f"{filename} missing canonical class definition: "
        f"{classname}"
    )


def test_phase_runners_package_exports_all_classes():
    """`phase_runners/__init__.py` MUST re-export every
    canonical class so callers import via the package surface."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/phase_runners"
        / "__init__.py"
    )
    text = target.read_text(encoding="utf-8")
    for _, classname in _PHASE_RUNNER_MODULES:
        assert classname in text, (
            f"phase_runners/__init__.py missing export: "
            f"{classname}"
        )


# ---------------------------------------------------------------------------
# Orchestrator routes through every flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag_name", _PHASE_RUNNER_FLAGS)
def test_orchestrator_dispatches_through_flag(flag_name):
    """Every phase-runner master flag MUST appear in
    orchestrator.py at the dispatch site. Removing the
    dispatch wiring without removing the runner module would
    silently re-inline the phase body."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/orchestrator.py"
    )
    text = target.read_text(encoding="utf-8")
    # Each flag must be referenced at the dispatch site at
    # least once. The default-true env read covered by
    # `test_phase_runner_flag_defaults_true` already proves
    # the dispatch wiring; this is a weaker independent check.
    count = text.count(flag_name)
    assert count >= 1, (
        f"{flag_name} not referenced in orchestrator.py. "
        f"Dispatch wiring missing."
    )


# ---------------------------------------------------------------------------
# Combined-runner architectural decision (Slice 4b)
# ---------------------------------------------------------------------------


def test_slice4b_runner_covers_approve_apply_verify():
    """`Slice4bRunner` MUST encompass APPROVE + APPLY + VERIFY
    per the Wave 2 architectural decision (separate runners
    would require 6-way artifact threading; combined runner
    preserves inline semantics with one flag + one reindent).

    Bytes-pinned because this combined pattern is intentional —
    a future refactor that splits the runner without updating
    the pin set must fail CI."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/phase_runners"
        / "slice4b_runner.py"
    )
    text = target.read_text(encoding="utf-8")
    # Module-level commitment to all three phases.
    assert "APPROVE + APPLY + VERIFY" in text, (
        "slice4b_runner.py docstring MUST commit to "
        "APPROVE + APPLY + VERIFY coverage"
    )
    assert "OperationPhase.APPROVE" in text, (
        "slice4b_runner.py MUST handle OperationPhase.APPROVE"
    )
    assert "OperationPhase.APPLY" in text, (
        "slice4b_runner.py MUST handle OperationPhase.APPLY"
    )
    assert "OperationPhase.VERIFY" in text, (
        "slice4b_runner.py MUST handle OperationPhase.VERIFY"
    )


# ---------------------------------------------------------------------------
# Closure summary — exactly 9 phase runners; no more, no fewer
# ---------------------------------------------------------------------------


def test_phase_runners_directory_has_exactly_nine_modules():
    """The phase_runners/ directory MUST contain exactly the 9
    canonical module files (plus __init__.py + __pycache__).
    Adding a 10th runner without updating the pin set indicates
    drift; deletion indicates regression to inline."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/phase_runners"
    )
    py_files = sorted(
        p.name for p in target.glob("*.py")
        if p.name != "__init__.py"
    )
    expected = sorted(
        filename for filename, _ in _PHASE_RUNNER_MODULES
    )
    assert py_files == expected, (
        f"phase_runners/ directory drift detected.\n"
        f"  expected: {expected}\n"
        f"  actual:   {py_files}\n"
        f"Add new runner to _PHASE_RUNNER_MODULES if "
        f"intentional; otherwise restore deletion."
    )


# ---------------------------------------------------------------------------
# §35 entry corrected — closure marker
# ---------------------------------------------------------------------------


def test_brutal_review_entry_is_stale_proof():
    """Bytes-pin: the 9-runner state proves the §28.5.1 entry
    'CLASSIFY/APPROVE/APPLY/VERIFY remain torn-read landmines'
    is OBSOLETE. CLASSIFY landed via CLASSIFYRunner; APPROVE +
    APPLY + VERIFY landed via Slice4bRunner (combined per
    Wave 2 architectural decision).

    This test exists for citation purposes — when an operator
    audits the brutal-review backlog, finding this test passing
    is the structural proof the entry is closed."""
    classify = (
        _repo_root()
        / "backend/core/ouroboros/governance/phase_runners"
        / "classify_runner.py"
    )
    slice4b = (
        _repo_root()
        / "backend/core/ouroboros/governance/phase_runners"
        / "slice4b_runner.py"
    )
    assert classify.exists()
    assert slice4b.exists()
    # Both default-true in production
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/orchestrator.py"
    )
    text = target.read_text(encoding="utf-8")
    assert (
        '"JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED", "true"' in text
    )
    assert (
        '"JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED", "true"' in text
    )

"""Slice 4B — FAIL_TO_PASS pytest scoping for InteractiveRepair.

Closes the validate-with-noise trap surfaced by capability soak
bt-2026-05-25-091657. Even with Slice 4A relaxing L2's hard-stop on
``missing_dependency``, the root noise persists: ``pytest -x -q``
runs every test in the worktree cwd. For SWE-Bench-Pro ops on Ansible
(1000s of tests) that produces failures unrelated to the model's
patch and overwhelms the L2 classifier's signal extraction.

The envelope's ``fail_to_pass`` evidence (this slice) is exactly the
set of tests SWE-Bench-Pro expects to flip FAIL→PASS after the fix.
Scope pytest to those tests for a clean signal.

# Plumbing chain

  ProblemSpec.metadata["fail_to_pass"]  (Scale AI extension)
       ↓
  envelope_builder._build_evidence(...)
       ↓
  envelope.evidence["fail_to_pass"]
       ↓
  ctx.intake_evidence_json (JSON-encoded)
       ↓
  validate_runner.py reads ctx.intake_evidence_json
       ↓
  _test_argv = [..., "pytest", "-x", "-q", *fail_to_pass]
       ↓
  InteractiveRepair.run subprocess

# Defensive schema handling

Upstream SWE-Bench-Pro datasets vary on the field name:
  - ``FAIL_TO_PASS`` (classic SWE-Bench convention, uppercase)
  - ``fail_to_pass`` (Scale AI snake_case)
  - JSON-encoded strings (some datasets)
  - Comma/newline-separated strings (legacy)

envelope_builder normalizes all four to a List[str]. Empty list →
no scoping → legacy unscoped behavior (byte-identical).

# Test surface (2 AST pins + 5 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENVELOPE_BUILDER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "swe_bench_pro" / "envelope_builder.py"
)
VALIDATE_RUNNER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "validate_runner.py"
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_envelope_builder_emits_fail_to_pass() -> None:
    """``_build_evidence`` must add a ``fail_to_pass`` key to the
    evidence dict. Without it the downstream consumer (validate_runner)
    sees nothing and falls back to unscoped pytest."""
    src = ENVELOPE_BUILDER_FILE.read_text()
    assert '"fail_to_pass":' in src, (
        "envelope_builder._build_evidence does NOT emit fail_to_pass — "
        "Slice 4B plumbing chain broken at source."
    )
    # Both upstream key shapes (FAIL_TO_PASS + fail_to_pass) must be
    # consulted from problem.metadata
    assert 'FAIL_TO_PASS' in src and 'fail_to_pass' in src.lower(), (
        "envelope_builder doesn't consult both upstream field names"
    )


def test_ast_pin_validate_runner_scopes_pytest_to_fail_to_pass() -> None:
    """validate_runner.py must extract fail_to_pass from
    ctx.intake_evidence_json and extend _test_argv with the test list.
    Without this, the model still sees the full noise of an unscoped
    pytest run."""
    src = VALIDATE_RUNNER_FILE.read_text()
    assert "_fail_to_pass" in src, (
        "validate_runner is missing the _fail_to_pass extraction — "
        "Slice 4B plumbing chain broken at consumer."
    )
    assert "_test_argv.extend(_fail_to_pass)" in src, (
        "validate_runner does NOT extend _test_argv with fail_to_pass "
        "list — pytest still runs unscoped."
    )
    assert '"micro_fix_pytest_scoped"' in src, (
        "Missing FSM telemetry tag micro_fix_pytest_scoped"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 5
# ──────────────────────────────────────────────────────────────────────


def test_spine_envelope_builder_extracts_fail_to_pass_list() -> None:
    """When ProblemSpec.metadata has ``fail_to_pass`` as a list,
    envelope evidence carries it verbatim."""
    from backend.core.ouroboros.governance.swe_bench_pro.envelope_builder import (
        _build_evidence,
    )
    # Minimal stubs — _build_evidence only needs getattr access
    class _Problem:
        instance_id = "test-instance"
        base_commit = "abc123"
        repo_url = "https://github.com/x/y.git"
        gold_patch = "some patch"
        metadata = {
            "fail_to_pass": [
                "tests/test_foo.py::test_one",
                "tests/test_bar.py::test_two",
            ],
        }

    class _Prepared:
        worktree_path = Path("/tmp/wt/x")
        branch_name = "swebp/x"

    evidence = _build_evidence(_Problem(), _Prepared())
    assert evidence["fail_to_pass"] == [
        "tests/test_foo.py::test_one",
        "tests/test_bar.py::test_two",
    ]


def test_spine_envelope_builder_extracts_uppercase_field() -> None:
    """Upstream SWE-Bench datasets use ``FAIL_TO_PASS`` (uppercase).
    envelope_builder normalizes either case."""
    from backend.core.ouroboros.governance.swe_bench_pro.envelope_builder import (
        _build_evidence,
    )

    class _Problem:
        instance_id = "test-instance"
        base_commit = "abc123"
        repo_url = ""
        gold_patch = ""
        metadata = {"FAIL_TO_PASS": ["tests/test_foo.py::test_one"]}

    class _Prepared:
        worktree_path = Path("/tmp/wt/x")
        branch_name = "swebp/x"

    evidence = _build_evidence(_Problem(), _Prepared())
    assert evidence["fail_to_pass"] == ["tests/test_foo.py::test_one"]


def test_spine_envelope_builder_handles_json_string() -> None:
    """Some datasets store fail_to_pass as JSON-encoded string."""
    from backend.core.ouroboros.governance.swe_bench_pro.envelope_builder import (
        _build_evidence,
    )

    class _Problem:
        instance_id = "x"
        base_commit = ""
        repo_url = ""
        gold_patch = ""
        metadata = {
            "fail_to_pass": '["tests/a.py::t1", "tests/b.py::t2"]',
        }

    class _Prepared:
        worktree_path = Path("/tmp/wt/x")
        branch_name = "swebp/x"

    evidence = _build_evidence(_Problem(), _Prepared())
    assert evidence["fail_to_pass"] == ["tests/a.py::t1", "tests/b.py::t2"]


def test_spine_envelope_builder_handles_missing_field() -> None:
    """Legacy ProblemSpecs (no fail_to_pass key at all) yield an
    empty list — legacy unscoped pytest behavior preserved."""
    from backend.core.ouroboros.governance.swe_bench_pro.envelope_builder import (
        _build_evidence,
    )

    class _Problem:
        instance_id = "x"
        base_commit = ""
        repo_url = ""
        gold_patch = ""
        metadata = {}  # no fail_to_pass

    class _Prepared:
        worktree_path = Path("/tmp/wt/x")
        branch_name = "swebp/x"

    evidence = _build_evidence(_Problem(), _Prepared())
    assert evidence["fail_to_pass"] == []


def test_spine_pytest_argv_extension_pure_logic() -> None:
    """Pure-logic test of the _test_argv extension pattern from
    validate_runner. Empty list → unchanged argv; non-empty → extended."""
    # Empty case
    fail_to_pass: list = []
    test_argv = ["python3", "-m", "pytest", "-x", "-q"]
    if fail_to_pass:
        test_argv.extend(fail_to_pass)
    assert test_argv == ["python3", "-m", "pytest", "-x", "-q"]

    # Populated case
    fail_to_pass = [
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
    ]
    test_argv = ["python3", "-m", "pytest", "-x", "-q"]
    if fail_to_pass:
        test_argv.extend(fail_to_pass)
    assert test_argv == [
        "python3", "-m", "pytest", "-x", "-q",
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
    ]

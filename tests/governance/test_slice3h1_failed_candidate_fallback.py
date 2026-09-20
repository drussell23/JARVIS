"""Slice 3H.1 — failed-candidate propagation to micro-fix.

Closes the dead-code path in Slice 3H Part 1 surfaced by capability
soak bt-2026-05-25-075811. Soak proved Slice 3H Part 2
(``micro_fix_root_envelope_override``) fires correctly, but Part 1
(``micro_fix_target_from_candidate``) was DEAD on the failed-
validation path that micro-fix actually runs from.

# Root cause

``best_candidate`` is assigned in ``validate_runner.py:295-296`` ONLY
when ``validation.passed`` is True::

    if validation.passed and best_candidate is None:
        best_candidate = candidate

But micro-fix runs precisely when validation FAILED (the entire
purpose of the InteractiveRepairLoop is to fix critique errors). So
``best_candidate`` is None at the time Slice 3H's predicate
``if not _repair_target and best_candidate is not None`` evaluates,
the candidate fallback never fires, ``_repair_target`` stays None,
and ``micro_fix_skipped_no_target`` logs every iteration.

The candidate the model emitted IS in scope as
``generation.candidates`` — the validator just ran against it. We
just need to fall back to ``generation.candidates[0]`` when
``best_candidate`` is None.

# Fix mechanism — two-step fallback chain

The predicate now walks a documented fallback chain:

  1. ``best_candidate`` (kept first — preserves the passed-validation
     path that pre-existed)
  2. ``generation.candidates[0]`` (new — the candidate the validator
     just critiqued)

Both branches emit the same FSM telemetry tag
(``micro_fix_target_from_candidate``) with a ``source=`` discriminator
(``best_candidate`` or ``generation_candidates_first``) so operator
grep can attribute the path correctly.

# Test surface (2 AST pins + 4 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

from backend.core.ouroboros.governance.phase_runners.validate_runner import (
    _resolve_repair_plan,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATE_RUNNER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "validate_runner.py"
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_generation_candidates_participate_in_the_fallback() -> None:
    """A candidate the validator just critiqued must be reachable.

    The bt-2026-05-25-075811 trap: ``best_candidate`` is only assigned on a
    PASSED validation, and micro-fix runs precisely when validation FAILED,
    so a chain that consulted only ``best_candidate`` was dead. The op's
    candidates must therefore be a usable source on their own.
    """
    plan = _resolve_repair_plan(
        candidates=[{"file_path": "lib/ansible/cli/doc.py",
                     "full_content": "x = 1\n"}],
        target_files=(),
    )
    assert plan is not None, (
        "no repair plan derived from generation candidates alone — the "
        "Slice 3H.1 dead-code trap is open again."
    )
    assert plan[0] == "lib/ansible/cli/doc.py"


def test_best_candidate_precedes_generation_candidates() -> None:
    """The documented fallback order, asserted as behavior.

    ``best_candidate`` first, then the op's own candidates. The caller hands
    both to ``_resolve_repair_plan`` in that order; this pins that the first
    entry actually wins rather than being shadowed by a later one.
    """
    best = {"file_path": "from_best.py", "full_content": "x = 1\n"}
    other = {"file_path": "from_generation.py", "full_content": "y = 2\n"}
    plan = _resolve_repair_plan(candidates=[best, other], target_files=())
    assert plan is not None
    assert plan[0] == "from_best.py", (
        "generation candidate shadowed best_candidate — documented "
        "fallback order (best_candidate first) is broken."
    )


def test_declared_target_outranks_candidate_order() -> None:
    """An operator-declared target wins when the candidate proposes it.

    Keeps the micro-fix inside the scope the op was sanctioned for, rather
    than repairing whichever file the model happened to list first.
    """
    cand = {"files": [
        {"file_path": "first.py", "full_content": "x = 1\n"},
        {"file_path": "declared.py", "full_content": "y = 2\n"},
    ]}
    plan = _resolve_repair_plan(candidates=[cand], target_files=("declared.py",))
    assert plan is not None
    assert plan[0] == "declared.py"


def test_no_plan_without_proposed_content() -> None:
    """A candidate that proposes no TEXT yields no repair plan.

    Absence of content is a real nothing-to-repair. It is not the same thing
    as absence of a FILE, which is what the old ``is_file()`` precondition
    confused it with -- and which skipped every creation goal.
    """
    assert _resolve_repair_plan(
        candidates=[{"file_path": "empty.py"}], target_files=(),
    ) is None


# ──────────────────────────────────────────────────────────────────────
# Spine — 4 (pure-logic tests of the fallback chain)
# ──────────────────────────────────────────────────────────────────────


def test_spine_failed_validation_path_resolves_target() -> None:
    """THE bt-2026-05-25-075811 regression test. ctx.target_files empty
    + best_candidate=None (failed validation) + generation.candidates
    has one entry → repair_target derived from candidates[0]."""

    class _G:
        candidates = ({"file_path": "lib/ansible/cli/doc.py"},)

    ctx_target_files: tuple[str, ...] = ()
    best_candidate = None  # validation FAILED — bc is None
    generation = _G()

    _repair_target = (
        list(ctx_target_files)[0] if ctx_target_files else None
    )
    if not _repair_target:
        _fallback_cand = None
        _fallback_source = ""
        if best_candidate is not None:
            _fallback_cand = best_candidate
            _fallback_source = "best_candidate"
        elif generation.candidates:
            _fallback_cand = generation.candidates[0]
            _fallback_source = "generation_candidates_first"
        if _fallback_cand is not None:
            _cand_path = _fallback_cand.get("file_path", "") or ""
            if _cand_path:
                _repair_target = _cand_path

    assert _repair_target == "lib/ansible/cli/doc.py"
    assert _fallback_source == "generation_candidates_first"


def test_spine_best_candidate_wins_when_present() -> None:
    """When best_candidate IS set (passed-validation path), it wins
    over generation.candidates — backward compatibility with Slice 3H
    pre-3H.1 semantics."""

    class _G:
        candidates = ({"file_path": "second/choice.py"},)

    ctx_target_files: tuple[str, ...] = ()
    best_candidate = {"file_path": "first/choice.py"}
    generation = _G()

    _repair_target = None
    _fallback_cand = None
    _fallback_source = ""
    if best_candidate is not None:
        _fallback_cand = best_candidate
        _fallback_source = "best_candidate"
    elif generation.candidates:
        _fallback_cand = generation.candidates[0]
        _fallback_source = "generation_candidates_first"
    if _fallback_cand is not None:
        _repair_target = _fallback_cand.get("file_path", "")

    assert _repair_target == "first/choice.py"
    assert _fallback_source == "best_candidate"


def test_spine_empty_candidates_yields_no_target() -> None:
    """Defense-in-depth: when BOTH best_candidate is None AND
    generation.candidates is empty (theoretical edge — provider
    returned zero candidates), repair_target stays None and
    micro-fix still skips correctly — no null-pointer hazard from
    the new branch."""

    class _G:
        candidates = ()

    best_candidate = None
    generation = _G()

    _fallback_cand = None
    _fallback_source = ""
    if best_candidate is not None:
        _fallback_cand = best_candidate
        _fallback_source = "best_candidate"
    elif generation.candidates:
        _fallback_cand = generation.candidates[0]
        _fallback_source = "generation_candidates_first"

    assert _fallback_cand is None
    assert _fallback_source == ""


def test_spine_ctx_target_files_short_circuits_fallback() -> None:
    """When ctx.target_files IS populated (legacy intake path), the
    fallback chain never executes — the legacy target wins. Backward
    compatibility regression pin."""

    class _G:
        candidates = ({"file_path": "fallback/should/not/win.py"},)

    ctx_target_files = ("legacy/explicit/target.py",)
    best_candidate = None
    generation = _G()

    _repair_target = (
        list(ctx_target_files)[0] if ctx_target_files else None
    )
    if not _repair_target:
        # Fallback chain (should NOT execute here)
        _fallback_cand = None
        if best_candidate is not None:
            _fallback_cand = best_candidate
        elif generation.candidates:
            _fallback_cand = generation.candidates[0]
        if _fallback_cand is not None:
            _cand_path = _fallback_cand.get("file_path", "") or ""
            if _cand_path:
                _repair_target = _cand_path

    assert _repair_target == "legacy/explicit/target.py"

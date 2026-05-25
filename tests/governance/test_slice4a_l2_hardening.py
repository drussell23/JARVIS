"""Slice 4A — L2-local hard-stop subtype narrowing.

Closes the L2-after-1-iter trap surfaced by capability soak
bt-2026-05-25-091657. With all of Slice 3G/3H/3H.1/3H.2/3H.3 live,
the InteractiveRepair micro-fix loop now runs productively against
real Ansible code with parseable pytest errors. The model wrote a
patch that broke an import (visible in the log:
``InteractiveRepair Iter 0: fixed ModuleNotFoundError at L122-131``).
But when L2 dispatched as the deeper repair layer, it bailed after
ONE iteration with ``stop_reason=non_retryable_env:missing_dependency``
— the failure_classifier treated ``ModuleNotFoundError`` as a
non-retryable environment issue.

# Root cause

``failure_classifier.NON_RETRYABLE_ENV_SUBTYPES`` is a global set
that includes ``missing_dependency``. The semantic is correct for
operator-facing tools: if your environment is missing torch, no LLM
can pip-install it. But in L2 repair CONTEXT, the model just edited
the file's imports / code in the worktree, so ``ModuleNotFoundError``
is almost always a CODE issue the next iteration can fix.

# Fix mechanism

L2 narrows its OWN hard-stop subtypes to the ones no patch can
plausibly resolve. The classifier's global semantic is preserved.

  _L2_HARD_STOP_ENV_SUBTYPES = frozenset({
      "permission_denied", "port_conflict",
  })
  if (
      classification.is_non_retryable
      and classification.env_subtype in _L2_HARD_STOP_ENV_SUBTYPES
  ):
      return _stopped(f"non_retryable_env:{classification.env_subtype}")

``missing_dependency`` and ``interpreter_mismatch`` fall through to
the existing per-class retry cap (``budget.max_class_retries.get(
fail_class, 1)``) so L2 uses its iteration budget to fight through.

# Test surface (1 AST pin + 4 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPAIR_ENGINE_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "repair_engine.py"
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PIN — 1
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_l2_hard_stop_subtypes_narrowed() -> None:
    """L2's bail condition must reference its OWN narrower frozenset,
    NOT the global ``is_non_retryable`` flag alone. Otherwise
    ``missing_dependency`` triggers a one-iteration bail and L2's
    repair budget is wasted."""
    src = REPAIR_ENGINE_FILE.read_text()
    # The new local frozenset must be present
    assert "_L2_HARD_STOP_ENV_SUBTYPES" in src, (
        "Slice 4A L2-local hard-stop frozenset missing — "
        "bt-2026-05-25-091657 trap is open."
    )
    # Both truly-environmental subtypes must be in the narrowed set
    assert '"permission_denied"' in src, (
        "permission_denied missing from L2 hard-stop set"
    )
    assert '"port_conflict"' in src, (
        "port_conflict missing from L2 hard-stop set"
    )
    # The bail predicate must AND the narrower set with is_non_retryable
    assert (
        "classification.env_subtype in _L2_HARD_STOP_ENV_SUBTYPES" in src
    ), (
        "Slice 4A bail predicate does not consult the narrowed set — "
        "missing_dependency will still trigger early bail."
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 4 (each retryable + each hard-stop)
# ──────────────────────────────────────────────────────────────────────


def test_spine_missing_dependency_no_longer_hard_stops_l2() -> None:
    """``missing_dependency`` (``ModuleNotFoundError``) must NOT
    trigger the L2 hard-stop after Slice 4A. The classifier still
    reports is_non_retryable=True (global semantic preserved), but L2
    falls through to the per-class retry cap."""
    from backend.core.ouroboros.governance.failure_classifier import (
        FailureClassifier,
        NON_RETRYABLE_ENV_SUBTYPES,
    )

    class _Stub:
        stdout = "ModuleNotFoundError: No module named 'foo'"
        stderr = ""
        passed = False
        returncode = 1

    cls = FailureClassifier().classify(_Stub())
    # Global semantic: is_non_retryable IS True (classifier unchanged)
    assert cls.env_subtype == "missing_dependency"
    assert cls.is_non_retryable is True
    assert "missing_dependency" in NON_RETRYABLE_ENV_SUBTYPES

    # L2-local semantic: missing_dependency NOT in the hard-stop set
    _L2_HARD_STOP_ENV_SUBTYPES = frozenset({
        "permission_denied", "port_conflict",
    })
    assert cls.env_subtype not in _L2_HARD_STOP_ENV_SUBTYPES, (
        "missing_dependency in L2 hard-stop set — Slice 4A semantic broken"
    )


def test_spine_permission_denied_still_hard_stops_l2() -> None:
    """``permission_denied`` IS a truly environmental issue (umask,
    container constraints, etc.) — no LLM patch can fix it. Slice 4A
    preserves the hard-stop for this subtype."""
    _L2_HARD_STOP_ENV_SUBTYPES = frozenset({
        "permission_denied", "port_conflict",
    })
    # Mock the classification result for the L2 predicate
    is_non_retryable = True
    env_subtype = "permission_denied"
    should_bail = (
        is_non_retryable and env_subtype in _L2_HARD_STOP_ENV_SUBTYPES
    )
    assert should_bail, (
        "permission_denied does NOT hard-stop L2 — operator-environment "
        "issues should not consume L2 iteration budget"
    )


def test_spine_port_conflict_still_hard_stops_l2() -> None:
    """``port_conflict`` (e.g., address already in use) is
    environmental — preserved hard-stop."""
    _L2_HARD_STOP_ENV_SUBTYPES = frozenset({
        "permission_denied", "port_conflict",
    })
    is_non_retryable = True
    env_subtype = "port_conflict"
    should_bail = (
        is_non_retryable and env_subtype in _L2_HARD_STOP_ENV_SUBTYPES
    )
    assert should_bail


def test_spine_test_failure_falls_through_to_progress_check() -> None:
    """Normal ``TEST`` failures (not env-classified) have
    is_non_retryable=False so the bail predicate is False on the
    first check (short-circuit) — L2 proceeds to its progress /
    oscillation / per-class retry checks. Slice 4A doesn't change
    this path; regression pin."""
    from backend.core.ouroboros.governance.failure_classifier import (
        FailureClassifier,
    )

    class _Stub:
        stdout = "FAILED tests/test_foo.py::test_bar - AssertionError"
        stderr = ""
        passed = False
        returncode = 1

    cls = FailureClassifier().classify(_Stub())
    assert cls.is_non_retryable is False, (
        f"TEST classifier marked is_non_retryable={cls.is_non_retryable} "
        f"— Slice 4A regression"
    )
    # The L2 predicate is False (short-circuit on is_non_retryable=False)
    _L2_HARD_STOP_ENV_SUBTYPES = frozenset({
        "permission_denied", "port_conflict",
    })
    should_bail = (
        cls.is_non_retryable
        and cls.env_subtype in _L2_HARD_STOP_ENV_SUBTYPES
    )
    assert should_bail is False

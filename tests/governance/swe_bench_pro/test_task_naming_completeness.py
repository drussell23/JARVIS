"""Slice 6 — task-naming completeness AST + behavioural pins.

The EvaluatorTraceObserver (Slices 1-4, PR #48711; ignition wired in
Slice 5, PR #50189) discriminates SWE-Bench-Pro work from the rest of
the asyncio task population by name prefix
(``swe_bench_pro:`` / ``evaluator:`` / ``scorer:`` / ``prepare:``).
Slice 6 closes the visibility cloak: every deep entry point in the
``swe_bench_pro`` module is now wrapped in a ``task_phase`` context
that renames the current asyncio task to the canonical
``swe_bench_pro:<phase>:<instance_id>`` for the duration of the phase.

This module pins two structural invariants:

1.  Every ``asyncio.create_task(...)`` call inside the entire
    ``swe_bench_pro`` package carries a ``name=`` kwarg, AND the
    kwarg's value composes to a string beginning with
    ``swe_bench_pro:`` (either a literal, an f-string starting with
    that prefix, or assigned from a local variable named ``_task_name``
    whose origin we can trace back to a ``swe_bench_pro:`` literal).

2.  Every async-def function in the closed
    ``_PHASE_GUARDED_FUNCTIONS`` set declares ``async with
    task_phase(EvaluatorPhase.<X>, ...)`` somewhere in its body. The
    EvaluatorPhase enum value is the single source of phase truth —
    raw string-literal phase names at the call site are forbidden by
    a separate pin so the taxonomy stays closed.

The scans are dynamic: they walk every ``.py`` file under
``backend/core/ouroboros/governance/swe_bench_pro/`` so a new module
landing in that package is auto-enrolled. No hardcoded file list, no
hardcoded line numbers — the invariants survive future refactors
of the module's internal organization."""

from __future__ import annotations

import ast
import asyncio
import pathlib
import sys
import unittest
from typing import Iterator, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Helpers — module discovery + AST walking
# ---------------------------------------------------------------------------

SWE_BENCH_PRO_DIR: pathlib.Path = (
    pathlib.Path(__file__).resolve().parents[3]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "swe_bench_pro"
)


def _iter_module_files() -> Iterator[pathlib.Path]:
    """Yield every .py file in the swe_bench_pro package (excluding
    __pycache__ + test-only files). Dynamic — a new module landing
    here is automatically enrolled in all Slice 6 invariants."""
    for path in sorted(SWE_BENCH_PRO_DIR.glob("*.py")):
        if path.name.startswith("__"):
            # __init__.py is mainly re-exports; we still scan it (no
            # exception is granted from the invariants).
            pass
        yield path


def _parse(path: pathlib.Path) -> ast.Module:
    """Parse a Python source file into an AST module. Raises a clear
    AssertionError if syntax is broken — the surrounding test will
    surface it as a real failure."""
    try:
        return ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover — should never fire
        raise AssertionError(
            f"swe_bench_pro module file {path} has invalid syntax: {exc}"
        ) from exc


def _find_create_task_calls(
    tree: ast.Module,
) -> List[Tuple[ast.Call, str]]:
    """Yield every ``asyncio.create_task(...)`` (and ``create_task(...)``
    invoked as a bare name — defensive — and ``loop.create_task(...)``
    on any receiver) Call node found in the AST, paired with a short
    string description of the callee for diagnostic messages."""
    out: List[Tuple[ast.Call, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        is_create_task = False
        # asyncio.create_task(...)
        if (
            isinstance(callee, ast.Attribute)
            and callee.attr == "create_task"
        ):
            is_create_task = True
        # bare create_task(...) — possible after `from asyncio import
        # create_task`. Scanned defensively.
        elif (
            isinstance(callee, ast.Name)
            and callee.id == "create_task"
        ):
            is_create_task = True
        if is_create_task:
            out.append((node, _safe_unparse(callee)))
    return out


def _safe_unparse(node: ast.AST) -> str:
    """``ast.unparse`` exists on Python 3.9+ (CLAUDE.md mandates that
    floor). Defensive fallback returns a generic stub for older
    interpreters, which never run in CI."""
    if hasattr(ast, "unparse"):
        return ast.unparse(node)  # type: ignore[attr-defined]
    return "<unparse-unavailable>"


def _name_kwarg_resolves_to_swe_bench_pro(
    call: ast.Call,
    module: ast.Module,
) -> Tuple[bool, str]:
    """Return ``(ok, diagnostic)``. ``ok`` is True iff the call has a
    ``name=`` kwarg whose value either:
      * is a string literal starting with ``"swe_bench_pro:"``, OR
      * is an f-string (JoinedStr) whose first FormattedValue/Str
        segment is a literal starting with ``"swe_bench_pro:"``, OR
      * references a local variable (Name node) that is module-level
        or function-local assigned from one of the above two forms in
        an enclosing scope, OR
      * is itself a ``compose_canonical_task_name(...)`` call (the
        canonical helper introduced by Slice 6, which guarantees the
        prefix structurally).

    The function intentionally errs on the strict side — if it cannot
    determine the prefix, it fails (caller can adjust the literal/
    helper to bring it into line)."""
    name_kw: Optional[ast.keyword] = None
    for kw in call.keywords:
        if kw.arg == "name":
            name_kw = kw
            break
    if name_kw is None:
        return False, "missing `name=` kwarg"

    value = name_kw.value
    # Direct literal: name="swe_bench_pro:..."
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        if value.value.startswith("swe_bench_pro:"):
            return True, "literal swe_bench_pro: prefix"
        return False, (
            f"literal name does not start with 'swe_bench_pro:' "
            f"(got {value.value!r})"
        )
    # f-string: name=f"swe_bench_pro:...{x}"
    if isinstance(value, ast.JoinedStr) and value.values:
        first = value.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value.startswith("swe_bench_pro:"):
                return True, "f-string with swe_bench_pro: prefix"
            return False, (
                f"f-string first segment does not start with "
                f"'swe_bench_pro:' (got {first.value!r})"
            )
        return False, (
            "f-string first segment is not a string literal — "
            "cannot statically prove swe_bench_pro: prefix"
        )
    # Helper call: name=compose_canonical_task_name(EvaluatorPhase.X, id)
    if isinstance(value, ast.Call):
        callee = value.func
        if (
            isinstance(callee, ast.Name)
            and callee.id == "compose_canonical_task_name"
        ):
            return True, "compose_canonical_task_name() helper"
        if (
            isinstance(callee, ast.Attribute)
            and callee.attr == "compose_canonical_task_name"
        ):
            return True, "compose_canonical_task_name() helper (qualified)"
    # Variable reference: name=_task_name (most common pattern)
    if isinstance(value, ast.Name):
        # Walk the enclosing function body for an assignment to that
        # variable whose RHS resolves to a swe_bench_pro: literal /
        # f-string / helper call.
        var_name = value.id
        for assigned in _find_assignments(module, var_name):
            if isinstance(assigned, ast.Constant) and isinstance(
                assigned.value, str
            ) and assigned.value.startswith("swe_bench_pro:"):
                return True, f"variable {var_name!r} ← literal"
            if isinstance(assigned, ast.JoinedStr) and assigned.values:
                first = assigned.values[0]
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and first.value.startswith("swe_bench_pro:")
                ):
                    return True, f"variable {var_name!r} ← f-string"
            if isinstance(assigned, ast.IfExp):
                # Defensive: ternary like
                #   _task_name = f"swe_bench_pro:..." if id else "swe_bench_pro:..."
                # Both branches must satisfy the invariant.
                body_ok, _ = _expr_resolves_to_swe_prefix(assigned.body)
                else_ok, _ = _expr_resolves_to_swe_prefix(assigned.orelse)
                if body_ok and else_ok:
                    return True, (
                        f"variable {var_name!r} ← if-expr (both branches "
                        f"satisfy swe_bench_pro: prefix)"
                    )
            if isinstance(assigned, ast.Call):
                callee = assigned.func
                if (
                    isinstance(callee, ast.Name)
                    and callee.id == "compose_canonical_task_name"
                ) or (
                    isinstance(callee, ast.Attribute)
                    and callee.attr == "compose_canonical_task_name"
                ):
                    return True, (
                        f"variable {var_name!r} ← "
                        f"compose_canonical_task_name()"
                    )
        return False, (
            f"variable {var_name!r} — cannot statically prove "
            f"swe_bench_pro: prefix (no qualifying assignment found)"
        )
    return False, (
        f"unsupported `name=` value expression: "
        f"{type(value).__name__}({_safe_unparse(value)!r})"
    )


def _expr_resolves_to_swe_prefix(node: ast.AST) -> Tuple[bool, str]:
    """Lightweight helper used by the if-expr branch checker."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node.value.startswith("swe_bench_pro:"):
            return True, "literal"
        return False, "literal-not-swe-prefix"
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and first.value.startswith("swe_bench_pro:")
        ):
            return True, "f-string"
        return False, "f-string-bad-prefix"
    return False, "unsupported"


def _find_assignments(
    module: ast.Module, var_name: str,
) -> List[ast.AST]:
    """Walk the module AST and return every RHS that has been
    assigned to ``var_name`` (across all functions / methods). This is
    intentionally coarse — we accept that a name-collision across
    functions could yield false positives, but in practice the
    swe_bench_pro module uses ``_task_name`` exactly twice
    (parallel_eval + harness_inject), both consistent with the
    invariant."""
    out: List[ast.AST] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    out.append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == var_name
                and node.value is not None
            ):
                out.append(node.value)
    return out


# ---------------------------------------------------------------------------
# Phase-guard taxonomy — closed set of functions Slice 6 wraps
# ---------------------------------------------------------------------------

# Frozen mapping from async-def name → expected EvaluatorPhase value.
# Adding a function to the swe_bench_pro module that should be
# trace-visible REQUIRES adding it here, which forces the author
# through code review with a paired phase. The pin below enforces
# that every member of this set has at least one ``task_phase(...)``
# call referencing the corresponding ``EvaluatorPhase.<X>``.
_PHASE_GUARDED_FUNCTIONS: frozenset = frozenset({
    # (func_name, expected EvaluatorPhase enum member name)
    ("prepare_problem",     "PREPARE_PROBLEM"),
    ("evaluate_problem",    "INGEST_ENVELOPE"),
    ("evaluate_problem",    "WAITING_TERMINAL"),
    ("score_evaluation",    "SCORE_EVALUATION"),
    ("record",              "RECORD_RESULT"),
})


def _find_async_def(
    module: ast.Module, func_name: str,
) -> Optional[ast.AsyncFunctionDef]:
    """Locate an async-def matching ``func_name`` anywhere in the
    module (including inside class bodies — ``EvaluationResultStore.
    record`` lives inside the class)."""
    for node in ast.walk(module):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            return node
    return None


def _has_task_phase_call_with_enum(
    func_node: ast.AsyncFunctionDef, enum_member: str,
) -> bool:
    """Return True iff the function body contains at least one
    ``task_phase(EvaluatorPhase.<enum_member>, ...)`` call. Walks
    every Call descendant (covers nested async-with blocks)."""
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name) and callee.id == "task_phase":
            ok, _ = _first_arg_is_evaluator_phase(node, enum_member)
            if ok:
                return True
        elif (
            isinstance(callee, ast.Attribute)
            and callee.attr == "task_phase"
        ):
            ok, _ = _first_arg_is_evaluator_phase(node, enum_member)
            if ok:
                return True
    return False


def _first_arg_is_evaluator_phase(
    call: ast.Call, enum_member: str,
) -> Tuple[bool, str]:
    """Inspect the first positional argument of a ``task_phase(...)``
    call: it must be ``EvaluatorPhase.<enum_member>``. This pin
    enforces the closed-taxonomy invariant — raw string phase names
    at the call site are forbidden."""
    if not call.args:
        return False, "task_phase called with no positional arg"
    first = call.args[0]
    if (
        isinstance(first, ast.Attribute)
        and isinstance(first.value, ast.Name)
        and first.value.id == "EvaluatorPhase"
        and first.attr == enum_member
    ):
        return True, "exact match"
    return False, (
        f"first arg is {_safe_unparse(first)} "
        f"(expected EvaluatorPhase.{enum_member})"
    )


# ===========================================================================
# AST pin 1 — every asyncio.create_task in swe_bench_pro has
# name= kwarg resolving to swe_bench_pro:
# ===========================================================================
#
# Documented META exemptions — task names that do NOT need the
# ``swe_bench_pro:`` prefix because they identify META-layer tasks
# (the observer observing the pipeline) rather than pipeline work
# itself. Adding to this set requires code review: the exemption
# weakens the invariant for ONE specific call site, by intent.
#
# Each entry: (file_basename, expected literal name string).
_META_EXEMPT_TASK_NAMES: frozenset = frozenset({
    # The EvaluatorTraceObserver's own self-spawn at
    # ``EvaluatorTraceObserver.start()`` is the meta-observer task,
    # not pipeline work. Giving it a ``swe_bench_pro:`` prefix would
    # cause it to self-appear in every trace frame (recursive
    # observability — confusing for forensic readers). The pin
    # exempts this single name and this single file.
    ("evaluator_trace_observer.py", "evaluator_trace_observer"),
})


def _is_meta_exempt(
    path: pathlib.Path, call: ast.Call,
) -> bool:
    """Return True iff the call site is a documented META exemption."""
    for kw in call.keywords:
        if kw.arg != "name":
            continue
        if (
            isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
            and (path.name, kw.value.value) in _META_EXEMPT_TASK_NAMES
        ):
            return True
    return False


class TestCreateTaskNameCompleteness(unittest.TestCase):
    """Dynamic AST scan: every ``asyncio.create_task(...)`` call
    inside the entire swe_bench_pro package must carry a ``name=``
    kwarg whose value composes to a string starting with
    ``swe_bench_pro:`` (or matches a documented META exemption — see
    ``_META_EXEMPT_TASK_NAMES``)."""

    def test_every_create_task_has_swe_bench_pro_name(self) -> None:
        offending: List[str] = []
        for path in _iter_module_files():
            module = _parse(path)
            for call_node, callee_repr in _find_create_task_calls(module):
                if _is_meta_exempt(path, call_node):
                    continue
                ok, diag = _name_kwarg_resolves_to_swe_bench_pro(
                    call_node, module,
                )
                if not ok:
                    offending.append(
                        f"{path.name}:{call_node.lineno}  "
                        f"{callee_repr}  →  {diag}"
                    )
        self.assertEqual(
            offending, [],
            "Slice 6 invariant violated — at least one "
            "asyncio.create_task in swe_bench_pro is missing a "
            "canonical swe_bench_pro: name. Offenders:\n  "
            + "\n  ".join(offending or ["(none)"])
        )

    def test_every_create_task_has_some_name_kwarg(self) -> None:
        """Universal invariant — ``name=`` kwarg must be present on
        EVERY create_task in the module (no exemption). This catches
        anonymous tasks before they hit the trace observer's filter."""
        unnamed: List[str] = []
        for path in _iter_module_files():
            module = _parse(path)
            for call_node, callee_repr in _find_create_task_calls(module):
                has_name = any(kw.arg == "name" for kw in call_node.keywords)
                if not has_name:
                    unnamed.append(
                        f"{path.name}:{call_node.lineno}  "
                        f"{callee_repr}  →  no name= kwarg"
                    )
        self.assertEqual(
            unnamed, [],
            "Anonymous asyncio.create_task found in swe_bench_pro — "
            "every spawn site must carry a name= kwarg.\n  "
            + "\n  ".join(unnamed or ["(none)"])
        )

    def test_meta_exempt_names_actually_present(self) -> None:
        """If a name appears in ``_META_EXEMPT_TASK_NAMES`` but the
        file no longer contains a create_task with that literal name,
        the exemption is dead and should be removed. Caught here so
        the exemption set never silently accumulates."""
        for fname, expected_literal in _META_EXEMPT_TASK_NAMES:
            path = SWE_BENCH_PRO_DIR / fname
            self.assertTrue(
                path.exists(),
                f"META exemption file {fname} no longer exists — "
                f"remove from _META_EXEMPT_TASK_NAMES",
            )
            module = _parse(path)
            found = False
            for call_node, _ in _find_create_task_calls(module):
                for kw in call_node.keywords:
                    if (
                        kw.arg == "name"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                        and kw.value.value == expected_literal
                    ):
                        found = True
                        break
                if found:
                    break
            self.assertTrue(
                found,
                f"META exemption ({fname}, {expected_literal!r}) is "
                f"dead — no create_task in that file uses that "
                f"literal name. Remove from _META_EXEMPT_TASK_NAMES."
            )

    def test_at_least_one_create_task_audited(self) -> None:
        """Sanity guard — if the dynamic scan finds zero
        ``create_task`` sites at all, the invariant is vacuously true
        and the pin contributes nothing. Fail loudly so a refactor
        that accidentally removes all task spawns is caught."""
        total = 0
        for path in _iter_module_files():
            module = _parse(path)
            total += len(_find_create_task_calls(module))
        self.assertGreaterEqual(
            total, 3,
            "Expected ≥3 asyncio.create_task call sites in "
            "swe_bench_pro (slice-6 baseline: harness_inject + "
            "parallel_eval + observer). Found "
            f"{total} — the scan may be broken or the module was "
            "gutted."
        )


# ===========================================================================
# AST pin 2 — every phase-guarded function has a task_phase wrapper
# pointing at the right EvaluatorPhase enum member
# ===========================================================================


class TestPhaseGuardedFunctionsWrapped(unittest.TestCase):
    """Every async-def listed in ``_PHASE_GUARDED_FUNCTIONS`` must
    declare ``async with task_phase(EvaluatorPhase.<X>, ...)`` in its
    body, for the phase it owns. This is the structural cage that
    makes the EvaluatorTraceObserver's inline-await visibility work."""

    def test_each_guarded_function_wraps_in_task_phase(self) -> None:
        missing: List[str] = []
        for func_name, expected_enum in sorted(_PHASE_GUARDED_FUNCTIONS):
            found_in: Optional[str] = None
            for path in _iter_module_files():
                module = _parse(path)
                func_node = _find_async_def(module, func_name)
                if func_node is None:
                    continue
                if _has_task_phase_call_with_enum(func_node, expected_enum):
                    found_in = path.name
                    break
            if found_in is None:
                missing.append(
                    f"async def {func_name}(...) — expected "
                    f"task_phase(EvaluatorPhase.{expected_enum}, ...) "
                    f"somewhere in its body"
                )
        self.assertEqual(
            missing, [],
            "Slice 6 phase-guard invariant violated:\n  "
            + "\n  ".join(missing or ["(none)"])
        )


# ===========================================================================
# Behavioural pins — task_phase actually renames the running task
# ===========================================================================


class TestTaskPhaseRenamesCurrentTask(unittest.IsolatedAsyncioTestCase):
    """Behavioural proof that ``task_phase`` does what the AST pins
    claim it does: rename the current asyncio task to the canonical
    ``swe_bench_pro:<phase>:<instance_id>`` for the duration of the
    block, then restore the prior name on exit."""

    async def test_rename_and_restore(self) -> None:
        from backend.core.ouroboros.governance.swe_bench_pro.evaluator_trace_observer import (  # noqa: E501
            EvaluatorPhase,
            task_phase,
        )

        async def runner() -> Tuple[str, str, str]:
            task = asyncio.current_task()
            assert task is not None
            task.set_name("outer-name")
            prior = task.get_name()
            async with task_phase(
                EvaluatorPhase.PREPARE_PROBLEM, "instance-X",
            ):
                inside = task.get_name()
            after = task.get_name()
            return prior, inside, after

        prior, inside, after = await asyncio.create_task(
            runner(), name="outer-name",
        )
        self.assertEqual(prior, "outer-name")
        self.assertEqual(
            inside, "swe_bench_pro:prepare_problem:instance-X",
            "task_phase should rename the task to "
            "swe_bench_pro:<phase>:<instance_id> inside the block"
        )
        self.assertEqual(
            after, "outer-name",
            "task_phase should restore the prior task name on exit"
        )

    async def test_failsoft_on_no_current_task(self) -> None:
        """When ``asyncio.current_task()`` is unavailable (synchronous
        / threaded contexts that bypass the event loop), task_phase
        is a strict no-op. We can only exercise this by mocking
        ``asyncio.current_task`` to return None — the production
        path always has a current task."""
        from backend.core.ouroboros.governance.swe_bench_pro import (
            evaluator_trace_observer,
        )
        original = asyncio.current_task

        def _none_current_task(loop: Optional[Any] = None) -> None:  # type: ignore[no-untyped-def]
            return None

        evaluator_trace_observer.asyncio.current_task = (  # type: ignore[attr-defined]
            _none_current_task
        )
        try:
            from backend.core.ouroboros.governance.swe_bench_pro.evaluator_trace_observer import (  # noqa: E501
                EvaluatorPhase,
                task_phase,
            )
            async with task_phase(
                EvaluatorPhase.RECORD_RESULT, "instance-Y",
            ):
                # No exception is the success condition.
                pass
        finally:
            evaluator_trace_observer.asyncio.current_task = original  # type: ignore[attr-defined]

    async def test_compose_canonical_task_name_format(self) -> None:
        """``compose_canonical_task_name`` is the single source of name
        composition. Format invariant: ``swe_bench_pro:<phase>:<id>``."""
        from backend.core.ouroboros.governance.swe_bench_pro.evaluator_trace_observer import (  # noqa: E501
            EvaluatorPhase,
            compose_canonical_task_name,
        )
        for phase in EvaluatorPhase:
            name = compose_canonical_task_name(phase, "abc-123")
            self.assertEqual(
                name, f"swe_bench_pro:{phase.value}:abc-123",
                f"compose_canonical_task_name format drifted for "
                f"{phase.name}",
            )


# ===========================================================================
# Public-surface pin — task_phase + compose_canonical_task_name are
# exported in __all__
# ===========================================================================


class TestPublicSurface(unittest.TestCase):
    """Slice 6 adds two new exports to ``evaluator_trace_observer.__all__``:
    ``task_phase`` and ``compose_canonical_task_name``. The pin
    prevents a future refactor from quietly removing them — every
    deep evaluator file imports them and a missing export would
    silently break the import wave."""

    def test_task_phase_in_all(self) -> None:
        from backend.core.ouroboros.governance.swe_bench_pro import (
            evaluator_trace_observer,
        )
        self.assertIn(
            "task_phase",
            evaluator_trace_observer.__all__,
            "task_phase must be exported in evaluator_trace_observer.__all__",
        )

    def test_compose_canonical_task_name_in_all(self) -> None:
        from backend.core.ouroboros.governance.swe_bench_pro import (
            evaluator_trace_observer,
        )
        self.assertIn(
            "compose_canonical_task_name",
            evaluator_trace_observer.__all__,
            "compose_canonical_task_name must be exported in "
            "evaluator_trace_observer.__all__",
        )


# Reuse ``Any`` to silence the no-untyped-def stub above without a
# direct import collision.
from typing import Any  # noqa: E402


if __name__ == "__main__":  # pragma: no cover
    sys.exit(unittest.main())

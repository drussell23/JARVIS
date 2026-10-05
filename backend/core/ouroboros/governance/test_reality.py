"""Test Reality Gate -- reject a hollow test candidate before pytest runs it.

## Why this exists

2026-10-04, soak bt-2026-10-05-012717: ``c985ccaee4`` landed
``tests/test_hardware_control.py`` -- one ``async def`` method inside a plain
``unittest.TestCase`` (unittest never awaits it, so it never executes) that
patched a non-existent ``src.hardware_control`` and asserted on its own mock.
pytest reported **1 passed**, even under ``-W error::RuntimeWarning``. VALIDATE
asks whether tests PASS; nothing asked whether they RAN or touched their
subject. Worse than adding nothing, the file now marks the module as covered,
so discovery will never send it real work.

## The invariants (structural, deterministic, before execution)

For every test function the candidate ADDS or CHANGES (unchanged legacy tests
are never judged -- existing debt must not block an unrelated edit):

1. ``cannot_execute`` -- an ``async def`` test inside a ``unittest.TestCase``
   that is not an ``IsolatedAsyncioTestCase`` is never awaited.
2. ``verifies_nothing`` -- it contains no verification: an ``assert``
   statement, an ``assert*`` call (``self.assertEqual``,
   ``mock.assert_called_once_with``), or ``pytest.raises``/``warns``/``fail``
   -- directly or through a helper defined in the same file that it calls
   (walked transitively, cycle-safe).

For the FILE, when the op names a subject module:

3. ``subject_not_imported`` -- the subject is imported (any spelling the
   repo's pytest ``pythonpath`` makes importable: ``backend.autonomy.x``,
   ``autonomy.x``, ``from backend.autonomy import x``, or
   ``importlib.import_module("...")``) AND something it binds is referenced.

Each violation carries the exact correction, rendered by :meth:`correction`
into the failure evidence that VALIDATE_RETRY and L2 read.

Pure AST; NEVER raises (an unparseable candidate is the syntax gate's
business, so it yields no verdict here).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

__all__ = [
    "FAILURE_CLASS",
    "RealityVerdict",
    "Violation",
    "analyze_test_source",
    "hollow_test_in_candidate",
]

#: The VALIDATE failure class for a candidate this gate rejects.
FAILURE_CLASS = "hollow_test"

_VERIFY_PYTEST_ATTRS = frozenset({"raises", "warns", "fail", "approx", "deprecated_call"})
_ASYNC_TESTCASE_BASES = frozenset({"IsolatedAsyncioTestCase", "AsyncTestCase", "AioHTTPTestCase"})


@dataclass(frozen=True)
class Violation:
    rule: str          # cannot_execute | verifies_nothing | subject_not_imported
    test: str          # qualified test name, or "<module>" for a file-level rule
    detail: str        # the precise correction for this violation


@dataclass(frozen=True)
class RealityVerdict:
    violations: Tuple[Violation, ...] = ()
    judged: Tuple[str, ...] = ()        # the new/changed tests that were checked
    subject: Optional[str] = None

    @property
    def hollow(self) -> bool:
        return bool(self.violations)

    def summary(self) -> str:
        rules = sorted({v.rule for v in self.violations})
        return f"{FAILURE_CLASS}: {', '.join(rules)} ({len(self.violations)} violation(s))"

    def correction(self) -> str:
        """The structural correction, phrased for the repair prompt."""
        if not self.violations:
            return ""
        lines = [
            "HOLLOW TEST -- rejected before execution by the Test Reality Gate. "
            "A test that cannot run, verifies nothing, or never touches its subject "
            "passes vacuously and blocks real coverage. Fix every item:",
        ]
        lines.extend(f"- {v.detail}" for v in self.violations)
        return "\n".join(lines)


def _is_test_name(name: str) -> bool:
    return name.startswith("test")


def _base_names(cls: ast.ClassDef) -> Set[str]:
    out: Set[str] = set()
    for base in cls.bases:
        name = getattr(base, "attr", None) or getattr(base, "id", None)
        if name:
            out.add(name)
    return out


def _is_unittest_class(cls: ast.ClassDef) -> bool:
    return any(b.endswith("TestCase") for b in _base_names(cls))


def _tests(tree: ast.Module) -> List[Tuple[str, ast.AST, Optional[ast.ClassDef]]]:
    """``(qualified name, node, owning class)`` for every collectable test."""
    out: List[Tuple[str, ast.AST, Optional[ast.ClassDef]]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_test_name(node.name):
            out.append((node.name, node, None))
        elif isinstance(node, ast.ClassDef) and (node.name.startswith("Test") or _is_unittest_class(node)):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_test_name(item.name):
                    out.append((f"{node.name}.{item.name}", item, node))
    return out


def _helpers(tree: ast.Module) -> Dict[str, ast.AST]:
    """Callable helpers a test may delegate verification to: module-level defs
    and methods (keyed by bare name -- ``self._check`` and ``_check`` alike)."""
    out: Dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node)
    return out


def _is_verification_call(call: ast.Call) -> bool:
    func = call.func
    name = getattr(func, "attr", None) or getattr(func, "id", None) or ""
    if name.startswith("assert") or name.startswith("assert_"):
        return True
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
            and func.value.id == "pytest" and func.attr in _VERIFY_PYTEST_ATTRS:
        return True
    return False


def _called_names(node: ast.AST) -> Iterable[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = getattr(sub.func, "attr", None) or getattr(sub.func, "id", None)
            if name:
                yield name


def _is_verifying_raise(node: ast.AST) -> bool:
    """``raise AssertionError(...)`` anywhere, or any ``raise`` guarded by an
    ``if`` -- a hand-written check is verification (measured: ``AssertionError``
    and conditional ``RuntimeError`` raises in 191 existing tests)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Raise) and sub.exc is not None:
            exc = sub.exc.func if isinstance(sub.exc, ast.Call) else sub.exc
            if (getattr(exc, "id", None) or getattr(exc, "attr", None)) == "AssertionError":
                return True
        if isinstance(sub, ast.If) and any(isinstance(s, ast.Raise) for b in (sub.body, sub.orelse) for s in b):
            return True
    return False


def _verifies(node: ast.AST, helpers: Dict[str, ast.AST], seen: Optional[Set[str]] = None) -> bool:
    seen = set() if seen is None else seen
    if _is_verifying_raise(node):
        return True
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            return True
        if isinstance(sub, ast.Call) and _is_verification_call(sub):
            return True
    for name in _called_names(node):
        helper = helpers.get(name)
        if helper is None or helper is node or name in seen:
            continue
        seen.add(name)
        if _verifies(helper, helpers, seen):
            return True
    return False


def _subject_bindings(tree: ast.Module, subject: str) -> Tuple[bool, Set[str]]:
    """Whether *subject* is imported, and the local names that import binds.

    A spelling counts when it equals the subject or is a dotted SUFFIX of it of
    at least two parts (``autonomy.x`` for ``backend.autonomy.x``, importable
    through the repo's ``pythonpath = . backend``).
    """
    def matches(module: str) -> bool:
        module = module.strip(".")
        if not module:
            return False
        if module == subject:
            return True
        return module.count(".") >= 1 and subject.endswith("." + module)

    imported = False
    bound: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if matches(alias.name):
                    imported = True
                    bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            if matches(node.module):
                imported = True
                bound.update(a.asname or a.name for a in node.names if a.name != "*")
                if any(a.name == "*" for a in node.names):
                    bound.add("*")
            else:
                for alias in node.names:
                    if matches(f"{node.module}.{alias.name}"):
                        imported = True
                        bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.Call):
            fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fname in ("import_module", "__import__") and node.args \
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) \
                    and matches(node.args[0].value):
                imported = True
                bound.add("*")
    return imported, bound


def _referenced(tree: ast.Module, names: Set[str]) -> bool:
    if "*" in names:
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names:
            return True
    return False


def _changed(tests, baseline: Optional[ast.Module]) -> List[Tuple[str, ast.AST, Optional[ast.ClassDef]]]:
    if baseline is None:
        return list(tests)
    before = {name: ast.dump(node) for name, node, _cls in _tests(baseline)}
    return [t for t in tests if before.get(t[0]) != ast.dump(t[1])]


def analyze_test_source(
    source: str,
    *,
    subject: Optional[str] = None,
    baseline_source: str = "",
) -> Optional[RealityVerdict]:
    """Judge a candidate test file. ``None`` when it cannot be parsed (the
    syntax gate owns that) or holds no tests at all. NEVER raises."""
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError):
        return None
    try:
        try:
            baseline = ast.parse(baseline_source) if baseline_source else None
        except (SyntaxError, ValueError):
            baseline = None
        tests = _tests(tree)
        if not tests:
            return None
        judged = _changed(tests, baseline)
        helpers = _helpers(tree)
        violations: List[Violation] = []
        for name, node, cls in judged:
            if isinstance(node, ast.AsyncFunctionDef) and cls is not None and _is_unittest_class(cls) \
                    and not (_base_names(cls) & _ASYNC_TESTCASE_BASES):
                violations.append(Violation(
                    "cannot_execute", name,
                    f"`{name}` is `async def` inside `{cls.name}(unittest.TestCase)`: unittest "
                    "never awaits it, so it never runs. Make it a module-level `async def "
                    f"{node.name}()` (this repo runs pytest with asyncio_mode=auto), or "
                    "subclass `unittest.IsolatedAsyncioTestCase`, or keep `def` and drive "
                    "the coroutine with `asyncio.run(...)`.",
                ))
            if not _verifies(node, helpers):
                violations.append(Violation(
                    "verifies_nothing", name,
                    f"`{name}` verifies nothing: it has no `assert`, no `assert*` call and no "
                    "`pytest.raises` -- directly or in a helper it calls. Assert on what the "
                    "subject RETURNS or DOES, not on a value the test itself configured.",
                ))
        if subject and judged:
            imported, bound = _subject_bindings(tree, subject)
            if not imported:
                violations.append(Violation(
                    "subject_not_imported", "<module>",
                    f"the file never imports its subject `{subject}`. Import it "
                    f"(`from {subject} import ...` or `import {subject}`) and exercise it; "
                    f"every `patch(...)` target must be a name inside `{subject}`.",
                ))
            elif not _referenced(tree, bound):
                violations.append(Violation(
                    "subject_not_imported", "<module>",
                    f"`{subject}` is imported but nothing it binds "
                    f"({', '.join(sorted(bound)) or '-'}) is ever used: the tests never touch "
                    "the subject. Call it and assert on the result.",
                ))
        return RealityVerdict(
            violations=tuple(violations),
            judged=tuple(n for n, _node, _c in judged),
            subject=subject,
        )
    except Exception:  # noqa: BLE001 -- a gate fault must never reject real work
        return None


def _op_subject(target_files: Sequence[str], description: str, repo_root) -> Optional[str]:
    """Dotted subject module of a test-synthesis op, through the same ladder the
    signature anchor and the exemplar injector use. ``None`` when the op does
    not write a new test for a resolvable subject."""
    try:
        from pathlib import Path as _Path  # noqa: PLC0415

        from backend.core.ouroboros.governance.ast_signature_anchor import (  # noqa: PLC0415
            _dotted_module, _import_label,
        )
        from backend.core.ouroboros.governance.test_exemplars import _subject_of_op  # noqa: PLC0415
        root = _Path(repo_root)
        subject = _subject_of_op(list(target_files or ()), description or "", root)
        return _dotted_module(_import_label(_Path(subject), root)) if subject else None
    except Exception:  # noqa: BLE001
        return None


def hollow_test_in_candidate(
    files: Sequence[Tuple[str, str]],
    *,
    target_files: Sequence[str],
    description: str,
    repo_root,
) -> Optional[Tuple[str, "RealityVerdict"]]:
    """``(path, verdict)`` for the first hollow TEST file a candidate proposes.

    The one entry point for every consumer (VALIDATE before pytest, L2 before
    its sandbox), so the rule cannot drift between them. Baselines come from
    disk: VALIDATE and L2 both run before APPLY, so the file on disk is still
    what the candidate replaces. The subject rule applies to a test file the op
    CREATES; an edit to an existing test file is judged on its changed tests
    only. NEVER raises; ``None`` means nothing hollow (or nothing judgeable).
    """
    try:
        from pathlib import Path as _Path  # noqa: PLC0415

        from backend.core.ouroboros.governance.ast_signature_anchor import (  # noqa: PLC0415
            is_test_path,
        )
        root = _Path(repo_root)
        subject = None
        subject_resolved = False
        for path, content in files or ():
            if not str(path).endswith(".py") or not is_test_path(str(path)):
                continue
            on_disk = root / str(path)
            try:
                baseline = on_disk.read_text(encoding="utf-8", errors="replace") if on_disk.is_file() else ""
            except OSError:
                baseline = ""
            if not baseline and not subject_resolved:
                subject = _op_subject(target_files, description, root)
                subject_resolved = True
            verdict = analyze_test_source(
                content or "", subject=None if baseline else subject, baseline_source=baseline,
            )
            if verdict is not None and verdict.hollow:
                return str(path), verdict
        return None
    except Exception:  # noqa: BLE001 -- a gate fault must never reject real work
        return None

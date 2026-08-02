"""How many tests can only ever pass? — the source-assertion rate.

A test that reads a source file and asserts a string appears in it cannot fail
for a behavioural reason. It fails when someone edits a comment and passes when
the feature behind it is dead. Three tests in this repository established that
``/narrate verbose`` worked by confirming the handler's SOURCE mentioned
``JARVIS_NARRATIVE_THINKING_VERBOSE`` — a flag no code read. They passed for the
entire period the feature did nothing, and they would have passed forever.

That is the mechanism that hid every inert capability the other three
instruments later found, so it deserves its own number.

WHAT IS AND IS NOT A DEFECT
-----------------------------
Source assertions are not wrong. This module's own test suite uses them, and so
does the fix for the comment-severing bug in ``providers.py`` — some invariants
are genuinely structural ("this decorator did not land inside a comment block")
and behaviour cannot express them.

The pathology is not the source assertion. It is the source assertion **as the
only evidence**:

    BEHAVIOURAL     no assertion touches source text
    STRUCTURAL_PIN  some do, some do not — the source assert BACKS a behaviour
    SOURCE_ONLY     every assertion touches source text; nothing was executed
    NO_ASSERTION    the function asserts nothing at all

``SOURCE_ONLY`` is the reportable class. A test in it is incapable of observing
the thing it names, which is a decidable property rather than a judgement, and
is why this is a measurement and not a lint.

WHY THIS ONE IS PINNED
------------------------
The producerless-sink detector — the fourth darkness class — was written, found
64 real cases, and left in a scratchpad with a note saying it would rot unless
promoted into ``tests/``. It was not promoted, and it is gone. An instrument
that measures dead code and is not itself covered is subject to its own
criterion. This module ships with its tests.

Reuses ``progress_board._iter_source_files`` for the walk rather than adding a
fourth tree-walker: that function already prunes at the directory and treats any
directory holding a ``.git`` entry as a checkout in its own right, which is what
keeps stale worktree copies from being counted twice.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import ast
import enum
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("Ouroboros.SourceAssertionAudit")

SOURCE_ASSERTION_SCHEMA_VERSION: str = "source_assertion_audit.v1"

_ENV_ENABLED = "JARVIS_SOURCE_ASSERTION_AUDIT_ENABLED"
_ENV_ROOTS = "JARVIS_SOURCE_ASSERTION_ROOTS"

#: Calls that hand back the text of a SOURCE file, unconditionally — the
#: receiver cannot be anything else.
_SOURCE_CALLS: frozenset = frozenset({
    "getsource",     # inspect.getsource — by far the most common here
    "getsourcelines",
})

#: ``.read_text()`` is CONDITIONAL and was the third false-positive class.
#: Reading a JSON/state artifact the test itself wrote and asserting on its
#: parsed contents is BEHAVIOURAL — it observes an effect of the code under
#: test. ``test_crash_recovery_persists_reset`` does exactly that and was
#: mis-flagged. It counts as source only when the path is demonstrably a
#: Python module: the receiver mentions ``__file__`` or a ``.py`` literal,
#: which is how both confirmed true positives address their target.
def _is_module_pathish(node: ast.AST) -> bool:
    """True if this expression addresses a Python SOURCE file. NEVER raises."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "__file__":
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "__file__":
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                and sub.value.strip().endswith(".py"):
            return True
    return False

#: ``.read()`` is CONDITIONAL and was the instrument's first false-positive
#: class. A bare ``.read()`` matches a PTY drain, a socket, a subprocess pipe
#: and an HTTP response — all of which are RUNTIME OUTPUT, so asserting on them
#: is behavioural, the exact opposite of what this module reports.
#: ``test_pty_makes_isatty_true_in_the_child`` was mis-flagged this way.
#: It counts as source only when the receiver is demonstrably a file opened in
#: this function.
_FILE_OPENERS: frozenset = frozenset({"open"})

#: Env-var-shaped literals. The `/narrate verbose` class asserted exactly this
#: shape into source text, so it is worth reporting separately from the general
#: rate: it is the sub-case where the test names a CONTRACT it never exercises.
_FLAGGISH = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,}$")


def audit_enabled() -> bool:
    """Master gate. Default TRUE — read-only static analysis, no side effects.
    NEVER raises."""
    return (os.environ.get(_ENV_ENABLED, "true") or "").strip().lower() not in (
        "0", "false", "no", "off",
    )


def audit_roots() -> Tuple[str, ...]:
    """Directories to scan. Default ``tests``. NEVER raises."""
    raw = (os.environ.get(_ENV_ROOTS, "") or "").strip()
    if not raw:
        return ("tests",)
    return tuple(p.strip() for p in raw.split(",") if p.strip())


class TestKind(str, enum.Enum):
    """What a test function is CAPABLE of observing."""

    BEHAVIOURAL = "behavioural"
    STRUCTURAL_PIN = "structural_pin"
    SOURCE_ONLY = "source_only"
    NO_ASSERTION = "no_assertion"


@dataclass(frozen=True)
class TestVerdict:
    """One test function's reading. Frozen — safe to share."""

    module: str
    name: str
    lineno: int
    kind: TestKind
    assertions: int
    source_assertions: int
    flag_literals: Tuple[str, ...] = ()

    @property
    def is_reportable(self) -> bool:
        return self.kind is TestKind.SOURCE_ONLY


@dataclass
class AuditReading:
    """Repo-wide reading. ``rate`` is the headline number."""

    schema_version: str = SOURCE_ASSERTION_SCHEMA_VERSION
    verdicts: List[TestVerdict] = field(default_factory=list)
    scanned_files: int = 0
    unparseable: List[str] = field(default_factory=list)

    def of_kind(self, kind: TestKind) -> List[TestVerdict]:
        return [v for v in self.verdicts if v.kind is kind]

    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def source_only(self) -> List[TestVerdict]:
        return self.of_kind(TestKind.SOURCE_ONLY)

    @property
    def rate(self) -> float:
        """Fraction of tests that can ONLY pass — the reported metric.

        Denominator is tests that assert at all: a test with no assertion is a
        different defect and counting it here would inflate this one.
        """
        asserting = [v for v in self.verdicts
                     if v.kind is not TestKind.NO_ASSERTION]
        if not asserting:
            return 0.0
        return len(self.source_only) / len(asserting)

    @property
    def flag_literal_tests(self) -> List[TestVerdict]:
        """SOURCE_ONLY tests that assert an env-var-shaped literal — the exact
        `/narrate verbose` class."""
        return [v for v in self.source_only if v.flag_literals]


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


def _source_bindings(fn: ast.AST) -> Set[str]:
    """Names bound to file text anywhere in *fn*.

    Deliberately flow-INSENSITIVE: a name bound to source text once is treated
    as source for the whole function. Under-precision here costs a false
    positive; the alternative (missing a rebind) costs a false negative, and a
    false negative is the failure direction this instrument exists to remove.
    """
    bound: Set[str] = set()
    handles: Set[str] = set()      # names bound to an OPEN FILE, not to text

    def _is_open_call(node: ast.AST) -> bool:
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _FILE_OPENERS)

    # Pass 1 — file handles. `with open(p) as fh` / `fh = open(p)`.
    for node in ast.walk(fn):
        if isinstance(node, ast.withitem) and node.optional_vars is not None:
            # ONLY `open(...)`. Binding every `with X() as y` was the second
            # false-positive class: `with console.capture() as cap` and
            # `with PtySession(...) as s` both made runtime capture look like
            # source text, so behavioural tests reported as SOURCE_ONLY.
            if _is_open_call(node.context_expr) \
                    and isinstance(node.optional_vars, ast.Name):
                handles.add(node.optional_vars.id)
        elif isinstance(node, ast.Assign) and _is_open_call(node.value):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    handles.add(t.id)

    def _yields_source(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if isinstance(f, ast.Attribute):
                if f.attr in _SOURCE_CALLS:
                    return True
                if f.attr == "read_text" and _is_module_pathish(f.value):
                    return True
                if f.attr == "read":
                    # Conditional: a file we opened, or `open(p).read()`.
                    if isinstance(f.value, ast.Name) and f.value.id in handles:
                        return True
                    if any(_is_open_call(x) for x in ast.walk(f.value)):
                        return True
            elif isinstance(f, ast.Name) and f.id in _SOURCE_CALLS:
                return True
        return False

    # Pass 2 — names bound to the TEXT itself.
    for node in ast.walk(fn):
        targets: List[ast.AST] = []
        if isinstance(node, ast.Assign) and _yields_source(node.value):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None \
                and _yields_source(node.value):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                bound.add(t.id)
    return bound


def _touches_source(node: ast.AST, bindings: Set[str]) -> bool:
    """True if this subtree reads source text — via a binding or inline."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in bindings:
            return True
        if isinstance(sub, ast.Call):
            f = sub.func
            nm = f.attr if isinstance(f, ast.Attribute) else (
                f.id if isinstance(f, ast.Name) else "")
            if nm in _SOURCE_CALLS:
                return True
            if nm == "read_text" and isinstance(f, ast.Attribute) \
                    and _is_module_pathish(f.value):
                return True   # `assert "x" in Path(__file__)...read_text()`
            # Inline `open(p).read()` only — a bare `.read()` on a pipe or a
            # PTY is runtime output, and asserting on THAT is behavioural.
            if nm == "read" and isinstance(f, ast.Attribute):
                if any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                       and x.func.id in _FILE_OPENERS
                       for x in ast.walk(f.value)):
                    return True
    return False


def _flag_literals(node: ast.AST) -> Tuple[str, ...]:
    out: List[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if _FLAGGISH.match(sub.value.strip()):
                out.append(sub.value.strip())
    return tuple(sorted(set(out)))


def _assertions(fn: ast.AST) -> List[ast.AST]:
    """Every assertion in *fn*, EXCLUDING nested function definitions.

    A helper defined inside a test carries its own assertions; attributing them
    to the enclosing test would let one behavioural helper launder a SOURCE_ONLY
    body into STRUCTURAL_PIN.
    """
    out: List[ast.AST] = []

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(child, ast.Assert):
                out.append(child)
            _walk(child)

    _walk(fn)
    return out


def classify(fn: ast.AST, module: str) -> Optional[TestVerdict]:
    """Classify one test function. Returns None if it is not a test. NEVER
    raises."""
    try:
        name = getattr(fn, "name", "")
        if not name.startswith("test_"):
            return None
        bindings = _source_bindings(fn)
        asserts = _assertions(fn)
        on_source = [a for a in asserts if _touches_source(a, bindings)]
        flags: Tuple[str, ...] = ()
        for a in on_source:
            flags = tuple(sorted(set(flags) | set(_flag_literals(a))))

        if not asserts:
            kind = TestKind.NO_ASSERTION
        elif not on_source:
            kind = TestKind.BEHAVIOURAL
        elif len(on_source) == len(asserts):
            kind = TestKind.SOURCE_ONLY
        else:
            kind = TestKind.STRUCTURAL_PIN

        return TestVerdict(
            module=module,
            name=name,
            lineno=int(getattr(fn, "lineno", 0) or 0),
            kind=kind,
            assertions=len(asserts),
            source_assertions=len(on_source),
            flag_literals=flags,
        )
    except Exception:  # noqa: BLE001 — an audit never breaks a run
        return None


def _test_functions(tree: ast.AST) -> Iterable[ast.AST]:
    """Top-level and class-method test functions, never nested helpers."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if getattr(node, "name", "").startswith("test_"):
                yield node


def audit(*, roots: Optional[Sequence[str]] = None) -> AuditReading:
    """Scan *roots* and classify every test function. NEVER raises."""
    reading = AuditReading()
    if not audit_enabled():
        return reading
    try:
        from backend.core.ouroboros.battle_test.progress_board import (
            _iter_source_files,
        )
        repo_root = Path(__file__).resolve().parents[4]
        for rel, path in _iter_source_files(repo_root, tuple(
                roots if roots is not None else audit_roots())):
            reading.scanned_files += 1
            try:
                tree = ast.parse(path.read_text(encoding="utf-8",
                                                errors="replace"))
            except Exception:  # noqa: BLE001
                reading.unparseable.append(rel)
                continue
            for fn in _test_functions(tree):
                v = classify(fn, rel)
                if v is not None:
                    reading.verdicts.append(v)
    except Exception:  # noqa: BLE001
        logger.debug("[SourceAssertion] audit degraded", exc_info=True)
    return reading


def render(reading: AuditReading, *, limit: int = 20) -> List[str]:
    """Operator-readable summary. NEVER raises."""
    try:
        counts: Dict[str, int] = {}
        for v in reading.verdicts:
            counts[v.kind.value] = counts.get(v.kind.value, 0) + 1
        lines = [
            f"source-assertion audit ({reading.schema_version})",
            f"  files {reading.scanned_files} · tests {reading.total}",
            "  " + " · ".join(f"{k} {counts.get(k, 0)}"
                              for k in (kind.value for kind in TestKind)),
            f"  RATE {reading.rate * 100:.2f}%  "
            f"({len(reading.source_only)} tests can only ever pass)",
        ]
        if reading.flag_literal_tests:
            lines.append(f"  flag-literal class: "
                         f"{len(reading.flag_literal_tests)}")
        for v in reading.source_only[:limit]:
            flags = (" [" + ",".join(v.flag_literals) + "]"
                     if v.flag_literals else "")
            lines.append(f"    {v.module}:{v.lineno} {v.name}{flags}")
        return lines
    except Exception:  # noqa: BLE001
        return ["source-assertion audit unavailable"]

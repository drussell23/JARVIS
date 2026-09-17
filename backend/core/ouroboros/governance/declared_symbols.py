"""Declared-symbol contract — signed intent outranks the model's "already done".

Why this exists (2026-09-07)
----------------------------
Goal ``ov-tier1-multi-003`` declares ``target_symbols``
(``test_tier1_multi_proof_persona`` / ``…_speech``) that do not exist yet.
The local model answered ``2b.1-noop`` — "the provided test files already
contain comprehensive test coverage" — and the pipeline honoured it: a
silent no-op COMPLETE for an operator-signed goal whose declared work was
never done, and (because no-op exits skipped the outcome seam) a goal
held "in flight" until the soak idled out.

A no-op is a *claim* that the change is already present. When the goal
DECLARES what must exist, that claim is checkable: every declared symbol
must be defined (function, class or method — any ``def``/``class`` in the
AST) in at least one target file on disk. If not, the no-op is refused and
the model retries with explicit feedback naming the missing symbols — the
same retry/feedback machinery every other GENERATE failure uses. The
mirror check applies to real candidates at VALIDATE: a candidate for a
goal with declared symbols must define them, or it fails before any test
runs. Pure AST, no model calls, never raises.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_ENV_ENABLED = "JARVIS_DECLARED_SYMBOL_CONTRACT_ENABLED"
_ENV_SCOPE_ENFORCE = "JARVIS_SURGICAL_SCOPE_ENFORCE"

#: The failure code a candidate earns for changing what it did not declare.
DIFF_SCOPE_VIOLATION = "diff_scope_violation"

#: Prefix of a violation raised against the module's own statements rather
#: than against a named definition. Callers distinguish these because the
#: module-level class is the NEWEST thing the validator can see and has never
#: been enforced in production.
MODULE_SCOPE_MARKER = "<module scope>"


def is_module_scope_violation(violation: str) -> bool:
    """Whether *violation* is about module-level statements, not a definition."""
    return str(violation or "").startswith(MODULE_SCOPE_MARKER)


def scope_verdict(violations: Sequence[str], *, enforced: bool) -> str:
    """What to DO about a set of scope violations: ``refuse``, ``calibrate`` or
    ``report``.

    Extracted so the policy is testable on its own. It lived inline in a
    14,000-line orchestrator method, which is where decisions go to stop being
    reviewable.

    * nothing found, or enforcement unarmed → ``report`` (log, continue);
    * every violation is module-level → ``calibrate``. That class is the newest
      thing the validator can see — the resolver only just learned to name
      bindings — so no production run has exercised it. It is credited with a
      ``ModuleScopeCalibrationEvent`` and allowed through, which is safe rather
      than merely hopeful: a module-level DELETION is still refused at
      promotion by the gate's structural check, a different check in a
      different process reading git;
    * anything symbol-level → ``refuse``, including a mixed verdict. The
      symbol-level half is already calibrated, and a candidate that breached
      both does not earn the benefit of the doubt for the half that is new.
    """
    if not violations or not enforced:
        return "report"
    if all(is_module_scope_violation(v) for v in violations):
        return "calibrate"
    return "refuse"


def contract_enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in ("0", "false", "no", "off")


def scope_enforced() -> bool:
    """Whether an out-of-scope change REFUSES a candidate, or is only reported.

    Default OFF, and the check runs either way: a brand-new refusal in the
    middle of VALIDATE can only be calibrated by watching what it would have
    refused. Arming it before that evidence exists is how a governor becomes a
    handbrake.
    """
    return os.environ.get(_ENV_SCOPE_ENFORCE, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def defined_names(source: str) -> frozenset:
    """Every def/class name anywhere in *source* (methods included).
    ``frozenset()`` when the source does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return frozenset()
    return frozenset(
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )


def _clean(symbols: Iterable[str]) -> Tuple[str, ...]:
    out: List[str] = []
    for s in symbols or ():
        s = str(s or "").strip()
        if s and s not in out:
            out.append(s)
    return tuple(out)


def missing_declared_symbols(symbols: Iterable[str], target_files: Iterable[str], project_root: Path) -> Tuple[str, ...]:
    """Declared symbols defined in NONE of the target files on disk. A
    missing/unreadable file defines nothing. ``()`` when nothing is declared
    or the contract is disabled. NEVER raises."""
    declared = _clean(symbols)
    if not declared or not contract_enabled():
        return ()
    try:
        present: set = set()
        for rel in target_files or ():
            p = Path(str(rel))
            if not p.is_absolute():
                p = Path(project_root) / p
            try:
                if p.is_file():
                    present |= defined_names(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        return tuple(s for s in declared if s not in present)
    except Exception:  # noqa: BLE001
        return ()


def _candidate_contents(candidate: Dict[str, Any]) -> List[str]:
    files = candidate.get("files")
    if isinstance(files, list) and files:
        return [f.get("full_content") for f in files if isinstance(f, dict) and isinstance(f.get("full_content"), str)]
    fc = candidate.get("full_content")
    return [fc] if isinstance(fc, str) else []


def symbols_missing_from_candidate(symbols: Iterable[str], candidate: Dict[str, Any]) -> Tuple[str, ...]:
    """Declared symbols the candidate does not define in any of its files.
    ``()`` when nothing is declared, the contract is disabled, or the
    candidate carries no full content (diff candidates are judged by the
    tests). NEVER raises."""
    declared = _clean(symbols)
    if not declared or not contract_enabled():
        return ()
    try:
        contents = _candidate_contents(candidate)
        if not contents:
            return ()
        present: set = set()
        for c in contents:
            present |= defined_names(c)
        return tuple(s for s in declared if s not in present)
    except Exception:  # noqa: BLE001
        return ()


def _node_dump(source: str, symbol: str) -> Optional[str]:
    """``ast.dump`` of the def/class named *symbol* (its last dotted
    segment) anywhere in *source*; None when absent or unparsable."""
    want = str(symbol).rsplit(".", 1)[-1]
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == want:
            return ast.dump(n)
    return None


def symbols_unchanged_in_candidate(
    symbols: Iterable[str], candidate: Dict[str, Any], original: Optional[str],
) -> Tuple[str, ...]:
    """Declared symbols whose definition in the candidate is semantically
    the ORIGINAL's (same AST): the goal asked for a change inside them and
    none was made. ``()`` when nothing is declared, the contract is
    disabled, the original is unknown, the candidate carries no full
    content, or the symbol is new (absent from the original). NEVER raises.

    Why: a swarm candidate landed as f97f8195d6 (2026-09-08) that differed
    from its parent only by the ASCII gate's punctuation rewrite and one
    comment typo — the multi-file decline the goal asked for was absent —
    and VALIDATE, the change engine and VERIFY all passed because nothing
    the candidate changed is observable by a test. Comments are not in the
    AST, so a comment-only edit is unchanged; a docstring edit is a change.
    """
    declared = _clean(symbols)
    if not declared or not contract_enabled() or not isinstance(original, str) or not original:
        return ()
    try:
        contents = _candidate_contents(candidate)
        if not contents:
            return ()
        unchanged: List[str] = []
        for sym in declared:
            before = _node_dump(original, sym)
            if before is None:
                continue
            after = None
            for content in contents:
                after = _node_dump(content, sym)
                if after is not None:
                    break
            if after is not None and after == before:
                unchanged.append(sym)
        return tuple(unchanged)
    except Exception:  # noqa: BLE001
        return ()


class _DefaultKwargPruner(ast.NodeTransformer):
    """Drop keyword arguments that restate a stdlib callable's own default.

    ``logging.exception(msg, exc_info=True)`` and ``logging.exception(msg)``
    call the same code with the same values — ``Logger.exception`` defaults
    ``exc_info`` to ``True``. A candidate that adds only that has changed the
    AST and changed nothing else, which is how soak bt-2026-09-17-184946
    produced a "real" delta out of nothing.

    ## Derived, never tabulated

    The defaults come from :func:`inspect.signature` of the actual callable.
    A hardcoded list of "redundant kwargs" would freeze one stdlib version's
    behaviour into a constant and rot silently — the same mistake the SDK
    surface shim exists to avoid.

    ## Three conditions, all required

    * the call is ``module.attr(...)`` where ``module`` is bound by a plain
      ``import module`` in this same file — a local name that merely looks
      like a module is not one;
    * that module is in :data:`sys.stdlib_module_names`. **Importing a project
      module to read its signature would execute project code during
      validation**, which is not a trade this check may make;
    * the passed value is a literal that compares equal to the default, with
      the same type — ``1`` is not ``True`` here, because a candidate that
      swapped one for the other changed the source for a reader even if Python
      would not notice.

    Anything unresolvable is left alone. The failure direction matters: a
    wrongly-pruned kwarg makes real work look trivial and refuses it, while a
    wrongly-kept one only lets a redundant change through to the tests.
    """

    def __init__(self, module_aliases: Dict[str, str]):
        self._aliases = module_aliases

    def visit_Call(self, node: ast.Call) -> ast.AST:  # noqa: N802
        self.generic_visit(node)
        try:
            defaults = self._defaults_for(node.func)
            if not defaults:
                return node
            kept = []
            for kw in node.keywords:
                if kw.arg is None or not isinstance(kw.value, ast.Constant):
                    kept.append(kw)
                    continue
                if kw.arg not in defaults:
                    kept.append(kw)
                    continue
                default = defaults[kw.arg]
                value = kw.value.value
                if type(default) is type(value) and default == value:
                    continue        # restates the default — semantically null
                kept.append(kw)
            node.keywords = kept
        except Exception:  # noqa: BLE001 — never let normalisation break a diff
            pass
        return node

    def _defaults_for(self, func: ast.AST) -> Dict[str, Any]:
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            return {}
        module_name = self._aliases.get(func.value.id)
        if not module_name:
            return {}
        import sys  # noqa: PLC0415

        root = module_name.split(".", 1)[0]
        if root not in getattr(sys, "stdlib_module_names", frozenset()):
            return {}
        import importlib  # noqa: PLC0415
        import inspect as _inspect  # noqa: PLC0415

        module = importlib.import_module(module_name)
        target = getattr(module, func.attr, None)
        if target is None or not callable(target):
            return {}
        out: Dict[str, Any] = {}
        for name, param in _inspect.signature(target).parameters.items():
            if param.default is not _inspect.Parameter.empty:
                out[name] = param.default
        return out


def _module_aliases(tree: ast.Module) -> Dict[str, str]:
    """``{local name: dotted module}`` for plain ``import x`` / ``import x as y``.

    ``from x import y`` is deliberately absent: it binds a callable, not a
    module, so ``y(...)`` is a bare call this pruner does not attempt.
    """
    out: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out[alias.asname or alias.name.split(".")[0]] = alias.name
    return out


def canonical_ast_dump(source: str) -> str:
    """``ast.dump`` of *source* with semantically null detail normalised away.

    Raises ``SyntaxError`` for unparsable input — the caller decides what an
    unparsable candidate means; it is not this function's verdict to give.
    """
    tree = ast.parse(source)
    tree = _DefaultKwargPruner(_module_aliases(tree)).visit(tree)
    ast.fix_missing_locations(tree)
    return ast.dump(tree)


def candidate_is_functional_noop(
    candidate: Dict[str, Any], original: Optional[str],
) -> bool:
    """Whether this candidate would change nothing a caller can observe.

    ## Why it is needed even though a no-op check already exists

    ``symbols_unchanged_in_candidate`` answers this ONLY for goals that declare
    symbols, and most do not — 22 of 28 roadmap goals carried none. So the
    common case had no no-op refusal at all, and soak bt-2026-09-17-180722
    committed the proof: an already-landed work order was re-emitted
    (``JARVIS_ALLOW_ROADMAP_REVISIT`` shadows seen hashes on purpose), the
    model correctly found the work already done, and the pipeline still
    produced ``7e7fe18c3c`` — a duplicated banner comment, quote churn and a
    stripped newline. Every gate downstream passed it, because none of them
    asked the simplest question.

    ## Threshold-free by construction

    No score, no bar, no "how much changed" — the normalized AST either differs
    or it does not. ``ast.dump`` already erases exactly what should not count:
    comments are absent from the AST entirely, whitespace and indentation are
    structure rather than text, quote style is unrepresented, and a trailing
    newline is invisible. Whatever survives that is something a caller could
    observe.

    A new file (no original) is never a no-op. An unparsable candidate is not
    judged here — syntax has its own gate, and failing it here would
    misattribute the refusal.

    NEVER raises; ``False`` on every uncertain path, because refusing real work
    is worse than letting a redundant candidate reach the tests that follow.
    """
    if not contract_enabled():
        return False
    if not isinstance(original, str) or not original:
        return False
    try:
        contents = _candidate_contents(candidate)
        if not contents:
            return False
        before = canonical_ast_dump(original)
        for content in contents:
            if canonical_ast_dump(content) != before:
                return False
        return True
    except (SyntaxError, ValueError):
        return False
    except Exception:  # noqa: BLE001
        return False


def _assign_names(node: ast.AST) -> Tuple[str, ...]:
    """Names bound by a module-level assignment statement, or ``()``."""
    targets: List[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    else:
        return ()
    out: List[str] = []
    stack = list(targets)
    while stack:
        t = stack.pop()
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            stack.extend(t.elts)
    return tuple(out)


def _binds_only(node: ast.AST, declared: set) -> bool:
    """Whether *node* is an assignment binding ONLY declared names.

    ``A = B = ...`` is in scope only when every name it binds was declared;
    half a declaration is not authorisation for the other half.
    """
    names = _assign_names(node)
    return bool(names) and all(n in declared for n in names)


def _module_residue(
    source: str, declared_bindings: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """``ast.dump`` of the module's own statements — everything at module level
    that is neither a def/class nor an import.

    This is where ``__all__`` lives, and module constants, and the module
    docstring: the parts of a file that belong to nobody's function and so are
    invisible to a per-symbol comparison. The deletion that prompted this
    whole check (``__all__``, 3 exports, in 7f8c686ce0) lives exactly here.

    Imports are excluded ON PURPOSE. The change that commit was asked to make
    genuinely required adding ``import logging`` at module level: a rule that
    forbids every line outside the target function would have rejected the
    good half of the work along with the bad. Scope has to be semantic, not
    positional.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    declared = set(_clean(declared_bindings or ()))
    kept = [
        n for n in tree.body
        if not isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                ast.Import, ast.ImportFrom),
        )
        # A DECLARED module-level binding is in scope by definition. Without
        # this, declaring `_FAILURE_MODE_DEFAULT` and then editing it would
        # report a violation for doing exactly what the goal authorised — the
        # resolver can now name such a target, so the validator has to honour
        # the declaration or the new reach only manufactures false positives.
        and not _binds_only(n, declared)
    ]
    return ast.dump(ast.Module(body=kept, type_ignores=[]))


def out_of_scope_changes(
    symbols: Iterable[str], candidate: Dict[str, Any], original: Optional[str],
) -> Tuple[str, ...]:
    """What this candidate changed that its goal never declared.

    The inverse of :func:`symbols_unchanged_in_candidate`, and the other half
    of the same contract: that one refuses a candidate that changed NOTHING it
    declared; this one refuses a candidate that changed something it did NOT.
    Both read the same ASTs through the same helpers — a second parser here
    would be a second thing to keep in agreement.

    ## The defect

    ``7f8c686ce0`` made its requested change (log before degrading in two
    broad ``except`` blocks) and, unannounced, also deleted the module's
    ``__all__``, de-indented a docstring continuation line and churned quote
    style — while its own message said "Keep behaviour otherwise identical".
    Every test passed, because nothing asserts ``__all__``. That is the
    whole-file re-emission signature: a model asked to reproduce a file
    reproduces MOST of it, and the drift lands wherever it lands.

    ## Why AST and not lines

    Comparison is by ``ast.dump`` per symbol plus the module residue, so:

    * a deleted export, a rewritten docstring or a changed constant IS a
      violation — those are semantic;
    * re-quoting ``(",", ":")`` as ``(',', ':')`` is NOT — the AST is
      identical, and it changes nothing any caller can observe;
    * adding an import the change requires is NOT — see
      :func:`_module_residue`.

    A line-level "nothing outside the target range" rule inverts all three:
    it flags the harmless churn, misses nothing semantic that the AST does
    not already catch, and blocks the legitimate import.

    ## What counts as in scope

    Declared symbols may change freely. Symbols ADDED by the candidate are
    allowed: a change routinely needs a new helper, and test synthesis is
    nothing but addition. Symbols REMOVED or MODIFIED without being declared
    are the violation.

    Returns the offending names, ``()`` when there is nothing to say — no
    declaration, contract disabled, unknown original, unparsable content, or
    a genuinely surgical candidate. NEVER raises.
    """
    declared = set(_clean(symbols))
    if not declared or not contract_enabled():
        return ()
    if not isinstance(original, str) or not original:
        return ()                       # a new file has no scope to exceed
    try:
        contents = _candidate_contents(candidate)
        if not contents:
            return ()
        content = next(
            (c for c in contents if ast.parse(c) is not None), None,
        )
    except Exception:  # noqa: BLE001 — unparsable candidates fail elsewhere
        return ()
    if content is None:
        return ()
    try:
        before_names = defined_names(original)
        after_names = defined_names(content)
        violations: List[str] = []
        # Symbols the candidate changed or dropped without declaring them.
        for name in sorted(before_names):
            if name in declared:
                continue
            before = _node_dump(original, name)
            if before is None:
                continue
            after = _node_dump(content, name)
            if after is None:
                violations.append(f"{name} (removed)")
            elif after != before:
                violations.append(f"{name} (modified)")
        # The module's own statements — where __all__ and constants live.
        # Declared bindings are excluded from BOTH sides: the goal named them,
        # so changing them is the work, not a breach.
        res_before = _module_residue(original, declared)
        res_after = _module_residue(content, declared)
        if (
            res_before is not None and res_after is not None
            and res_before != res_after
        ):
            violations.append(
                f"{MODULE_SCOPE_MARKER} (module-level statements changed)",
            )
        return tuple(violations)
    except Exception:  # noqa: BLE001
        return ()


def scope_feedback(violations: Sequence[str], symbols: Iterable[str]) -> str:
    """The retry instruction for an out-of-scope candidate.

    Names what was touched and what the goal actually authorised, in the same
    shape as :func:`refusal_feedback`, because it is consumed by the same L2
    re-prompt path.
    """
    declared = ", ".join(_clean(symbols)) or "(none declared)"
    touched = ", ".join(str(v) for v in violations)
    return (
        "Your change modified code outside its declared scope: "
        f"{touched}. This goal authorises changes to {declared} ONLY. "
        "Re-emit the file with those definitions changed and EVERY other "
        "definition, the module docstring and module-level statements such as "
        "__all__ reproduced exactly as they are. Adding an import your change "
        "requires is allowed."
    )


def refusal_feedback(missing: Sequence[str], target_files: Iterable[str]) -> str:
    """The retry instruction for a refused no-op — names exactly what must
    be added and where; nothing else changes."""
    files = ", ".join(str(f) for f in target_files or ()) or "the target files"
    return (
        "NO-OP REFUSED — the goal DECLARES symbols that do not exist yet: "
        + ", ".join(missing)
        + f". They must be ADDED to {files}. The change is NOT already present; "
        "do not return 2b.1-noop again. Return the full file content with every "
        "existing line preserved verbatim and the declared definitions appended at the end."
    )

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


def _module_residue(source: str) -> Optional[str]:
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
    kept = [
        n for n in tree.body
        if not isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                ast.Import, ast.ImportFrom),
        )
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
        res_before = _module_residue(original)
        res_after = _module_residue(content)
        if (
            res_before is not None and res_after is not None
            and res_before != res_after
        ):
            violations.append("<module scope> (module-level statements changed)")
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

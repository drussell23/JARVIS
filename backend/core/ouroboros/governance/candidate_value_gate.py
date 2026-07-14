# backend/core/ouroboros/governance/candidate_value_gate.py
"""
Semantic value gate — AST-proven cosmetic NO_OP detection (Slice 13).

Run-22 operator verdict: the only autonomously-landed commit was a duplicate
stale-torch comment appended to requirements.txt — the pipeline spent
GENERATE + VALIDATE (+14s candidate tree) + APPLY + VERIFY + AutoCommit +
promotion on a change with ZERO executable weight. This gate terminates such
candidates immediately after GENERATE as a benign ``no_op_cosmetic``
completion.

Classification is a PROOF, not a heuristic (mandate 1):

* ``.py`` — parse old and new with ``ast``; strip docstrings with the
  EXISTING ``epistemic_shedder._DocstringStripper`` (mandate 3 — the
  substrate audit disqualified ``duplication_checker._Normalizer``, which
  blanks constants and renames variables and would therefore classify REAL
  value changes as cosmetic); compare
  ``ast.dump(include_attributes=False)``. Comments and formatting never
  reach the AST, so equality mathematically proves no executable-logic
  mutation. Any inequality — one constant byte, one operator — is
  SUBSTANTIVE.
* Declared line-grammar formats (``requirements*.txt``, ``constraints*.txt``,
  ``*.cfg``, ``*.ini``) — these have no Python AST; their grammar defines
  whole-line ``#``/``;`` comments and blank lines as non-semantic. Stripping
  ONLY whole lines (never trailing fragments — string-content traps) and
  comparing the residue in order is the format's own normalization. This is
  the Run-22 noise class.
* Everything else — INDETERMINATE.

Fail-safe FORWARD (mandate 4): SyntaxError on either side, unreadable or
missing old file (a creation), unknown formats, or ANY substantive file in a
multi-file candidate → the op proceeds to VALIDATE untouched. Only a
candidate whose EVERY file is PROVEN cosmetic terminates.

Pure, zero-LLM, stdlib-only. Master switch (read at the orchestrator seam):
``JARVIS_CANDIDATE_VALUE_GATE_ENABLED`` (default true).
"""
from __future__ import annotations

import ast
import difflib
import fnmatch
import logging
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

COSMETIC = "cosmetic"
SUBSTANTIVE = "substantive"
INDETERMINATE = "indeterminate"

# Formats whose grammar declares whole-line comments. NEVER code files.
# Requirements-class formats ADDITIONALLY strip trailing comments per pip's
# own grammar ('#' at line start or preceded by whitespace begins a comment;
# '#' glued to content — e.g. VCS #egg= fragments — is content). Run-23
# hole: the model's ASCII em-dash→hyphen rewrites inside trailing comments
# on requirement lines read as substantive under whole-line-only stripping.
# ini/cfg values may legitimately contain '#', so they stay whole-line-only.
_REQUIREMENTS_GLOBS: Tuple[str, ...] = (
    "requirements*.txt", "constraints*.txt",
)
_LINE_GRAMMAR_GLOBS: Tuple[str, ...] = _REQUIREMENTS_GLOBS + (
    "*.cfg", "*.ini",
)
_LINE_COMMENT_PREFIXES: Tuple[str, ...] = ("#", ";")


def _python_ast_fingerprint(source: str) -> str:
    """``ast.dump`` of the docstring-stripped tree — raises on SyntaxError
    (callers map that to INDETERMINATE)."""
    from backend.core.ouroboros.governance.epistemic_shedder import (
        _DocstringStripper,
    )

    tree = ast.parse(source)
    stripped = _DocstringStripper().visit(tree)
    return ast.dump(stripped, include_attributes=False)


def _strip_pip_trailing_comment(line: str) -> str:
    """Remove a trailing comment per pip's requirements grammar: a comment
    begins at a ``#`` that is preceded by whitespace (or starts the line —
    handled by the whole-line pass). A ``#`` glued to content (VCS
    ``#egg=`` fragments) is content and survives. Single left-to-right
    scan — grammar normalization, not a regex band-aid."""
    for idx, ch in enumerate(line):
        if ch == "#" and (idx == 0 or line[idx - 1] in (" ", "\t")):
            return line[:idx].rstrip()
    return line


def _line_grammar_residue(source: str, *, strip_trailing: bool) -> List[str]:
    """Semantic residue per the declared grammar: whole-line comments and
    blank lines removed; ``strip_trailing`` (requirements-class only)
    additionally removes whitespace-preceded trailing comments; everything
    else kept verbatim, in order."""
    residue: List[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in _LINE_COMMENT_PREFIXES):
            continue
        if strip_trailing:
            line = _strip_pip_trailing_comment(line)
            if not line.strip():
                continue
        residue.append(line.rstrip())
    return residue


def _is_bare_string_expr(node: ast.AST) -> bool:
    """A bare string-literal statement — a docstring or stray string
    expression. Never executable logic (evaluated and discarded)."""
    return (
        isinstance(node, ast.Expr)
        and isinstance(getattr(node, "value", None), ast.Constant)
        and isinstance(node.value.value, str)
    )


def _stmt_count(node: ast.AST) -> int:
    """Executable statements contained in ``node`` (inclusive), EXCLUDING
    non-executable filler: bare string-literal expressions (docstrings /
    stray strings — evaluated and discarded) and ``pass`` (a no-op the
    docstring stripper itself inserts when a stripped body would be
    empty). A docstring-only module therefore censuses to ZERO — it holds
    no logic to defend."""
    return sum(
        1 for n in ast.walk(node)
        if isinstance(n, ast.stmt)
        and not isinstance(n, ast.Pass)
        and not _is_bare_string_expr(n)
    )


def _stripped_tree(source: str) -> ast.Module:
    from backend.core.ouroboros.governance.epistemic_shedder import (
        _DocstringStripper,
    )

    return _DocstringStripper().visit(ast.parse(source))


def _python_semantic_weight(old: str, new: str) -> int:
    """Executable-AST delta magnitude (Slice 15 mandate 1): SequenceMatcher
    over the docstring-stripped top-level statement dumps; each non-equal
    opcode contributes the LARGER side's contained-statement count. 0 ⇔
    the Slice-13 cosmetic proof (identical fingerprints)."""
    t_old, t_new = _stripped_tree(old), _stripped_tree(new)
    d_old = [ast.dump(s, include_attributes=False) for s in t_old.body]
    d_new = [ast.dump(s, include_attributes=False) for s in t_new.body]
    weight = 0
    sm = difflib.SequenceMatcher(None, d_old, d_new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        w_old = sum(_stmt_count(s) for s in t_old.body[i1:i2])
        w_new = sum(_stmt_count(s) for s in t_new.body[j1:j2])
        weight += max(w_old, w_new)
    return weight


def _residue_delta(old_res: List[str], new_res: List[str]) -> int:
    """Changed semantic-residue lines (line-grammar formats)."""
    weight = 0
    sm = difflib.SequenceMatcher(None, old_res, new_res, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        weight += max(i2 - i1, j2 - j1)
    return weight


def file_semantic_weight(
    root: Path, rel_path: str, new_content: str,
) -> Optional[int]:
    """THE weight engine (Slice 15) — semantic weight of one proposed file
    change against the CURRENT file at ``root/rel_path``.

    * ``0``    — mathematically cosmetic (the Slice-13 proof).
    * ``N>0``  — count of executable AST statements added/removed/changed
                 (Python), or changed grammar-residue lines (declared
                 line-grammar formats). File creation weighs its full
                 statement count (min 1).
    * ``None`` — indeterminate (syntax error either side, unknown format,
                 unreadable) — consumers MUST fail safe (mandate 4:
                 BACKGROUND route / pass-forward), never guess.

    Never raises.
    """
    try:
        target = Path(root) / rel_path
        name = Path(rel_path).name
        try:
            old_content = target.read_text(errors="replace")
        except OSError:
            # Creation (or unreadable-old): real change by definition.
            if name.endswith(".py"):
                try:
                    return max(1, _stmt_count(_stripped_tree(new_content)))
                except SyntaxError:
                    return None
            return max(1, len((new_content or "").splitlines()))
        if old_content == new_content:
            return 0
        if name.endswith(".py"):
            try:
                return _python_semantic_weight(old_content, new_content)
            except SyntaxError:
                return None
        if any(fnmatch.fnmatch(name, g) for g in _LINE_GRAMMAR_GLOBS):
            _trailing = any(
                fnmatch.fnmatch(name, g) for g in _REQUIREMENTS_GLOBS
            )
            return _residue_delta(
                _line_grammar_residue(old_content, strip_trailing=_trailing),
                _line_grammar_residue(new_content, strip_trailing=_trailing),
            )
        return None
    except Exception:  # noqa: BLE001 — fail-safe, never crash a caller
        logger.debug(
            "[ValueGate] weight error for %s — indeterminate",
            rel_path, exc_info=True,
        )
        return None


def candidate_semantic_weight(
    root: Path,
    files: Sequence[Tuple[str, str]],
) -> Tuple[Optional[int], List[Tuple[str, Optional[int]]]]:
    """Aggregate weight for a candidate: sum of per-file weights, or
    ``None`` when ANY file is indeterminate (poison — mandate-4 fail-safe;
    consumers route indeterminate work to BACKGROUND, never escalate on a
    guess). Returns ``(total, [(rel_path, weight), ...])``."""
    detail: List[Tuple[str, Optional[int]]] = []
    if not files:
        return None, detail
    total = 0
    poisoned = False
    for rel, content in files:
        w = file_semantic_weight(root, str(rel), content or "")
        detail.append((str(rel), w))
        if w is None:
            poisoned = True
        else:
            total += w
    return (None if poisoned else total), detail


def classify_file_change(root: Path, rel_path: str, new_content: str) -> str:
    """Slice-13 verdict, now a thin wrapper over the ONE weight engine
    (Slice 15 mandate 3): weight 0 → COSMETIC, N>0 → SUBSTANTIVE,
    None → INDETERMINATE. Never raises."""
    w = file_semantic_weight(root, rel_path, new_content)
    if w is None:
        return INDETERMINATE
    return COSMETIC if w == 0 else SUBSTANTIVE


def evaluate_candidate_value(
    root: Path,
    files: Sequence[Tuple[str, str]],
) -> Tuple[str, List[Tuple[str, str]]]:
    """Aggregate verdict for a candidate's ``(rel_path, new_content)`` pairs.

    SUBSTANTIVE if ANY file is substantive (one byte of executable change
    passes the gate — mandate 4). COSMETIC only when files exist and EVERY
    one is proven cosmetic. Otherwise INDETERMINATE (pass forward).
    Returns ``(verdict, [(rel_path, per_file_verdict), ...])``.
    """
    detail: List[Tuple[str, str]] = []
    if not files:
        return INDETERMINATE, detail
    saw_indeterminate = False
    for rel, content in files:
        v = classify_file_change(root, str(rel), content or "")
        detail.append((str(rel), v))
        if v == SUBSTANTIVE:
            return SUBSTANTIVE, detail
        if v == INDETERMINATE:
            saw_indeterminate = True
    if saw_indeterminate:
        return INDETERMINATE, detail
    return COSMETIC, detail

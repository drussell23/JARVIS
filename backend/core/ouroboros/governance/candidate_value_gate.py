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
import fnmatch
import logging
from pathlib import Path
from typing import List, Sequence, Tuple

logger = logging.getLogger(__name__)

COSMETIC = "cosmetic"
SUBSTANTIVE = "substantive"
INDETERMINATE = "indeterminate"

# Formats whose grammar declares whole-line comments. NEVER code files.
_LINE_GRAMMAR_GLOBS: Tuple[str, ...] = (
    "requirements*.txt", "constraints*.txt", "*.cfg", "*.ini",
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


def _line_grammar_residue(source: str) -> List[str]:
    """Semantic residue per the declared grammar: whole-line comments and
    blank lines removed; everything else kept verbatim, in order."""
    residue: List[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in _LINE_COMMENT_PREFIXES):
            continue
        residue.append(line.rstrip())
    return residue


def classify_file_change(root: Path, rel_path: str, new_content: str) -> str:
    """Classify one proposed file change against the CURRENT file at
    ``root/rel_path``. Returns COSMETIC / SUBSTANTIVE / INDETERMINATE.
    Never raises."""
    try:
        target = Path(root) / rel_path
        try:
            old_content = target.read_text(errors="replace")
        except OSError:
            return SUBSTANTIVE  # creation (or unreadable) — real change
        if old_content == new_content:
            # Byte-identical (fast path; same proof state_drift.file_sha256
            # gives the promotion exemption — no weaker than the AST route).
            return COSMETIC

        name = Path(rel_path).name
        if name.endswith(".py"):
            try:
                if (_python_ast_fingerprint(old_content)
                        == _python_ast_fingerprint(new_content)):
                    return COSMETIC
                return SUBSTANTIVE
            except SyntaxError:
                return INDETERMINATE
        if any(fnmatch.fnmatch(name, g) for g in _LINE_GRAMMAR_GLOBS):
            if (_line_grammar_residue(old_content)
                    == _line_grammar_residue(new_content)):
                return COSMETIC
            return SUBSTANTIVE
        return INDETERMINATE
    except Exception:  # noqa: BLE001 — fail-safe FORWARD, never crash GENERATE
        logger.debug(
            "[ValueGate] classification error for %s — indeterminate",
            rel_path, exc_info=True,
        )
        return INDETERMINATE


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

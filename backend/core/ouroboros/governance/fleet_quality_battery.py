"""In-memory AST validation armor for the FleetEvaluator subsystem.

This is the security boundary of the FleetEvaluator: it validates model
output by *parsing* it to an AST and inspecting the tree. It NEVER executes,
exec()s, eval()s, compiles-and-runs, or otherwise interprets the text it
validates. It performs NO I/O, NO network, NO file writes.

This module is a deliberate leaf: it imports only ``ast`` and ``re`` so it can
be loaded in sandboxes where the full organism cannot be imported. Every
public function is pure and never raises on None/empty/garbage input.
"""

from __future__ import annotations

import ast
import re

# --- Probe prompts -----------------------------------------------------------

# Codegen probe: a concrete, verifiable task. The downstream caller detects the
# codegen probe via the lowercased substring "code block"; the trailing "ONLY"
# sentence forces a clean single-block response.
CODEGEN_PROMPT: str = (
    "Implement two Python functions with full docstrings:\n"
    "  1. merge_intervals(intervals): given a list of [start, end] intervals, "
    "return a new list with all overlapping intervals merged, sorted by start. "
    "Handle the empty list (return []).\n"
    "  2. interval_union(a, b): given two intervals a=[s, e] and b=[s, e], "
    "return their union as a list of intervals (one if they overlap or touch, "
    "two if disjoint).\n"
    "Do not leave any function body as a placeholder. "
    "Return ONLY a single python code block."
)

# Classification probe: a task that is unambiguously ENRICH (add detail to an
# existing artifact, not a no-op, redirect, or net-new generation).
CLASSIFY_PROMPT: str = (
    "Classify the following development task as exactly one of: "
    "NO_OP | REDIRECT | ENRICH | GENERATE.\n"
    "Task: enrich the README with usage examples and a quickstart section "
    "for the functions that already exist in the module.\n"
    "Reply with ONLY the label."
)

EXPECTED_LABEL: str = "ENRICH"

# --- Code-block extraction ---------------------------------------------------

_CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n?(.*?)```", re.DOTALL)
_TODO_RE = re.compile(r"#\s*(TODO|implement|fill in|your code)", re.IGNORECASE)


def extract_code_block(text: str) -> str:
    """Return the contents of a fenced ```python ... ``` block, else the text.

    Falls back to the stripped raw text when no fenced block is present.
    Never raises; treats None as empty.
    """
    if not text:
        return ""
    match = _CODE_BLOCK_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


# --- Syntactic validation (parse only — never execute) -----------------------


def is_ast_valid(text: str) -> bool:
    """True iff the extracted code block parses as valid Python.

    Parses with ``ast.parse`` only. Never calls exec/eval/compile-and-run, so
    even a syntactically valid malicious payload is inspected, not executed.
    """
    code = extract_code_block(text)
    if not code:
        return False
    try:
        ast.parse(code)
        return True
    except (SyntaxError, ValueError, RecursionError, TypeError):
        return False


# --- Semantic placeholder detection ------------------------------------------


def _is_ellipsis_expr(node: ast.stmt) -> bool:
    """True if node is a bare ``...`` expression statement."""
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and node.value.value is Ellipsis
    )


def _is_notimplemented_raise(node: ast.stmt) -> bool:
    """True for ``raise NotImplementedError`` or ``raise NotImplementedError(...)``."""
    if not isinstance(node, ast.Raise) or node.exc is None:
        return False
    exc = node.exc
    # raise NotImplementedError(...)
    if isinstance(exc, ast.Call):
        exc = exc.func
    # raise NotImplementedError
    return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"


def _is_placeholder_body(body: list[ast.stmt]) -> bool:
    """True if a function body is exactly one placeholder statement."""
    if len(body) != 1:
        return False
    stmt = body[0]
    return (
        _is_ellipsis_expr(stmt)
        or isinstance(stmt, ast.Pass)
        or _is_notimplemented_raise(stmt)
    )


def has_semantic_placeholder(text: str) -> bool:
    """True if the code contains a placeholder body, bare ellipsis, or TODO.

    A docstring-only function body is NOT a placeholder. Never raises.
    """
    code = extract_code_block(text)
    if not code:
        return False
    try:
        tree = ast.parse(code)
    except Exception:
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_placeholder_body(node.body):
                return True
        elif _is_ellipsis_expr(node):
            # Module-level (or any-level) bare ellipsis expression statement.
            return True

    if _TODO_RE.search(code):
        return True

    return False


def code_quality_pass(text: str) -> bool:
    """True iff the code parses AND has no semantic placeholder."""
    return is_ast_valid(text) and not has_semantic_placeholder(text)


# --- Classification label adherence ------------------------------------------


def label_adherence(text: str, expected: str) -> float:
    """Score how well a classification response adheres to the expected label.

    1.0 for an exact (case/punctuation-insensitive) match, 0.5 for the label
    appearing within prose, 0.0 otherwise (including empty input). Never raises.
    """
    t = (text or "").strip().upper().strip(".!:\"' ")
    e = (expected or "").strip().upper()
    if not t or not e:
        return 0.0
    if t == e:
        return 1.0
    if e in t.split() or e in t:
        return 0.5
    return 0.0

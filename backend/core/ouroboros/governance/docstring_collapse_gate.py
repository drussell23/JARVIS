"""Iron Gate 4 — Docstring multi-line collapse detector.

Catches a model regression where a multi-line ``\"\"\"docstring\"\"\"`` is
rewritten as a single source-line literal containing ``\\n`` escape
sequences. The result parses cleanly (it is valid Python), so the AST
gate, ASCII gate, and dependency gate all pass it. The cosmetic damage
is severe: the file becomes unreadable, every newline is escape-encoded,
and downstream tooling that surfaces docstrings (help(), Sphinx, IDE
hover) renders ``\\n`` literally.

Triggering example (battle test bt-2026-04-11-211131, headless_cli.py)::

    \"\"\"\\nHeadless CLI — One-shot Ouroboros governance...\\n\\nGap 4: Run...\"\"\"

The detector is deterministic, AST-based, and runs in O(n) on the
candidate source. It hard-rejects through the GENERATE retry loop with
targeted feedback so the model rewrites the docstring properly.

Decision rule
-------------
A candidate is rejected when:

1. Its parsed AST contains a module/class/function-level docstring whose
   source span is a single line (``lineno == end_lineno``), and
2. The decoded docstring value contains an actual newline character
   (i.e. the source had ``\\n`` escapes that the parser decoded), and
3. EITHER the target file does not yet exist, OR the corresponding
   docstring in the original file was multi-line.

Condition (3) prevents false positives on legacy single-line docstrings
that already contain ``\\n`` escapes — we don't punish a candidate for
mirroring what was already on disk, only for actively collapsing one.

Multi-file candidates (``files: [...]``) are walked entry by entry; the
first offense found is returned.
"""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_GATE_ENABLED = (
    os.environ.get("JARVIS_DOCSTRING_COLLAPSE_GATE_ENABLED", "true").lower() != "false"
)


def _qualname_for_node(node: ast.AST, ancestors: Tuple[ast.AST, ...]) -> str:
    """Build a dotted qualified name for a class/function node.

    Module-level docstrings use the sentinel ``<module>``. Nested classes
    and functions are joined with ``.`` (e.g. ``Outer.Inner.method``).
    """
    if isinstance(node, ast.Module):
        return "<module>"
    parts: List[str] = []
    for anc in ancestors:
        if isinstance(anc, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(anc.name)
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        parts.append(node.name)
    return ".".join(parts) if parts else "<module>"


def _docstring_node(
    node: ast.AST,
) -> Optional[ast.Expr]:
    """Return the leading-string Expr node that serves as a docstring, if any.

    Mirrors the heuristic used by ``ast.get_docstring`` but returns the
    raw Expr node so we can read its source span. Handles ast.Constant
    (3.8+) which is the only string-literal node shape on supported
    Python versions.
    """
    body = getattr(node, "body", None)
    if not body:
        return None
    first = body[0]
    if not isinstance(first, ast.Expr):
        return None
    val = first.value
    if isinstance(val, ast.Constant) and isinstance(val.value, str):
        return first
    return None


def _walk_docstrings(
    tree: ast.Module,
) -> Dict[str, Tuple[ast.Expr, str]]:
    """Map ``qualname → (Expr node, decoded string)`` for every docstring.

    Walks the tree iteratively to maintain ancestor context for qualname
    construction. Includes the module-level docstring under ``<module>``.
    """
    out: Dict[str, Tuple[ast.Expr, str]] = {}
    stack: List[Tuple[ast.AST, Tuple[ast.AST, ...]]] = [(tree, ())]
    while stack:
        node, ancestors = stack.pop()
        ds_expr = _docstring_node(node)
        if ds_expr is not None:
            qname = _qualname_for_node(node, ancestors)
            # ast.Constant.value carries the decoded string.
            val_node = ds_expr.value
            if isinstance(val_node, ast.Constant) and isinstance(val_node.value, str):
                out[qname] = (ds_expr, val_node.value)
        # Recurse into nested classes/functions only — we don't care about
        # docstrings inside lambda expressions, comprehensions, etc.
        body = getattr(node, "body", None)
        if isinstance(body, list):
            new_anc = ancestors + (node,) if isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ) else ancestors
            for child in body:
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    stack.append((child, new_anc))
    return out


def _is_collapsed(ds_expr: ast.Expr, value: str) -> bool:
    """A docstring is 'collapsed' iff its source spans one line AND its
    decoded value contains a newline character.

    Both conditions matter: a deliberately short ``\"\"\"one liner\"\"\"`` has
    no newline in value (passes); a multi-line ``\"\"\"\\n...\\n\"\"\"`` written
    properly across many source lines also passes.
    """
    end_lineno = getattr(ds_expr, "end_lineno", ds_expr.lineno)
    if end_lineno != ds_expr.lineno:
        return False
    return "\n" in value


def _multiline_docstring_keys(tree: ast.Module) -> set:
    """Set of qualnames whose docstring spans more than one source line.

    Used to scope the gate: a candidate is only rejected for collapsing a
    docstring that was *previously* multi-line.
    """
    out: set = set()
    for qname, (ds_expr, _val) in _walk_docstrings(tree).items():
        end = getattr(ds_expr, "end_lineno", ds_expr.lineno)
        if end > ds_expr.lineno:
            out.add(qname)
    return out


def check_python_source(
    candidate_content: str,
    source_content: Optional[str],
) -> Optional[Tuple[str, List[str]]]:
    """Check a single .py file's candidate content against its prior source.

    Returns ``None`` if the candidate is fine. Otherwise returns
    ``(reason, offender_descriptions)`` where each offender is a string
    like ``"<qualname> @ Lline"`` for retry feedback.
    """
    if not _GATE_ENABLED or not candidate_content:
        return None

    try:
        new_tree = ast.parse(candidate_content)
    except SyntaxError:
        # Let the AST validator surface this — not our concern here.
        return None

    new_docs = _walk_docstrings(new_tree)
    if not new_docs:
        return None

    collapsed: List[Tuple[str, int]] = []
    for qname, (expr, value) in new_docs.items():
        if _is_collapsed(expr, value):
            collapsed.append((qname, expr.lineno))

    if not collapsed:
        return None

    # Scope by prior state: only reject collapses where the original was
    # multi-line (or where there is no original at all).
    if source_content:
        try:
            old_tree = ast.parse(source_content)
            old_multiline = _multiline_docstring_keys(old_tree)
        except SyntaxError:
            old_multiline = set()
        offenders_filtered = [
            (q, ln) for (q, ln) in collapsed if q in old_multiline
        ]
    else:
        # New file — every collapse is an offense (the convention is multi-line).
        offenders_filtered = collapsed

    if not offenders_filtered:
        return None

    descriptions = [f"{q} @ L{ln}" for (q, ln) in offenders_filtered]
    reason = (
        f"docstring_collapse: {len(offenders_filtered)} multi-line docstring(s) "
        f"replaced with single-line ``\\n`` escapes. The decoded value still "
        f"contains newlines, but the source line is collapsed — produce a "
        f"properly formatted multi-line ``\\\"\\\"\\\"`` docstring instead. "
        f"Offenders: {', '.join(descriptions[:5])}"
        f"{' ...' if len(descriptions) > 5 else ''}"
    )
    return reason, descriptions


def check_candidate(
    candidate: Dict[str, Any],
    repo_root: Any,
) -> Optional[Tuple[str, List[str]]]:
    """Orchestrator entry point — walks a candidate dict and dispatches per file.

    Handles both the legacy single-file shape (``full_content`` + ``file_path``)
    and the multi-file shape (``files: [{file_path, full_content}, ...]``).
    Returns the FIRST offense found, or None. Mirrors the surface area of
    ``dependency_file_gate.check_candidate`` so the orchestrator wires both
    the same way.
    """
    if not _GATE_ENABLED or not isinstance(candidate, dict):
        return None

    try:
        repo = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    except Exception:
        return None

    def _check_pair(file_path: str, cand_content: str) -> Optional[Tuple[str, List[str]]]:
        if not file_path or not cand_content:
            return None
        if not file_path.endswith(".py"):
            return None
        src_path = repo / file_path
        source_content: Optional[str]
        if src_path.exists():
            try:
                source_content = src_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                source_content = None
        else:
            source_content = None
        return check_python_source(cand_content, source_content)

    # Legacy single-file
    fp = candidate.get("file_path", "") or ""
    fc = candidate.get("full_content", "") or ""
    if fp and fc:
        res = _check_pair(fp, fc)
        if res is not None:
            return res

    # Multi-file
    files = candidate.get("files")
    if isinstance(files, list):
        for entry in files:
            if not isinstance(entry, dict):
                continue
            res = _check_pair(
                entry.get("file_path", "") or "",
                entry.get("full_content", "") or "",
            )
            if res is not None:
                return res

    return None

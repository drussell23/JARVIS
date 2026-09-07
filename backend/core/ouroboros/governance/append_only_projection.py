"""Append-only projection — an "add tests" candidate cannot touch existing code.

Why this exists (2026-09-07)
----------------------------
Goal ``ov-tier1-multi-003`` ("append test_tier1_multi_proof_* to two existing
test files") passed VALIDATE and then every APPLY died on
``guardian hard finding on tests/…/test_persona.py``. Replaying the
SemanticGuardian on the real candidate: ``test_assertion_weakened`` — the
model's FULL-FILE rewrite had "tidied" an existing assertion
(```"```" not in user``` → ```"`" not in user```) while appending its new
test. The guardian was right; the candidate SHAPE was wrong: a full-file
rewrite is the wrong vehicle for an append, and a 30B will drift existing
lines every few rewrites.

This projection realigns the candidate with the task by construction. For
a test-authoring op whose target file already exists, the candidate is
projected onto ``pristine old file + the candidate's NEW top-level
definitions (+ new imports / module assignments)``: every edit, rewrite or
removal of an existing definition is dropped and reported. The projection
runs BEFORE VALIDATE (validated content == applied content) and only when
the candidate actually adds something and actually touched existing code;
a pure append or a candidate with nothing new passes through untouched, a
repair op (production code in the candidate set) is never projected —
the same discriminator the differential VALIDATE gate uses. Pure AST,
never raises; on any parse failure the candidate is left as-is for the
guardian to judge.
"""
from __future__ import annotations

import ast
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.Orchestrator")

_ENV_ENABLED = "JARVIS_APPEND_ONLY_PROJECTION_ENABLED"


def projection_enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Projection:
    content: str
    added: Tuple[str, ...]        # new top-level names appended
    dropped: Tuple[str, ...]      # existing names the candidate edited/removed (edits discarded)
    changed: bool                 # projection differs from the candidate


def _top_level_defs(tree: ast.Module) -> Dict[str, ast.stmt]:
    out: Dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = node
    return out


def _assign_names(node: ast.stmt) -> Tuple[str, ...]:
    if isinstance(node, ast.Assign):
        return tuple(t.id for t in node.targets if isinstance(t, ast.Name))
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return (node.target.id,)
    return ()


def _segment(source: str, node: ast.stmt) -> str:
    """Source text of a top-level statement including leading decorators."""
    lines = source.splitlines(keepends=True)
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
    end = getattr(node, "end_lineno", node.lineno)
    return "".join(lines[start:end])


def _norm(node: ast.stmt) -> str:
    try:
        return ast.dump(node, include_attributes=False)
    except Exception:  # noqa: BLE001
        return repr(node)


def project_append_only(old: str, new: str) -> Projection:
    """Project *new* onto ``old + new definitions``. NEVER raises."""
    try:
        old_tree = ast.parse(old)
        new_tree = ast.parse(new)
    except (SyntaxError, ValueError):
        return Projection(new, (), (), False)
    old_defs = _top_level_defs(old_tree)
    new_defs = _top_level_defs(new_tree)
    old_imports = {_norm(n) for n in old_tree.body if isinstance(n, (ast.Import, ast.ImportFrom))}
    old_assigns = {nm for n in old_tree.body for nm in _assign_names(n)}

    dropped: List[str] = []
    for name, node in old_defs.items():
        if name not in new_defs:
            dropped.append(name)                      # removal discarded
        elif _norm(new_defs[name]) != _norm(node):
            dropped.append(name)                      # edit discarded
    new_import_nodes = [
        n for n in new_tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom)) and _norm(n) not in old_imports
    ]
    additions: List[ast.stmt] = []
    for node in new_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name not in old_defs:
                additions.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = _assign_names(node)
            if names and not any(nm in old_assigns for nm in names):
                additions.append(node)
    added_names = tuple(getattr(n, "name", None) or "/".join(_assign_names(n)) for n in additions)
    if not additions or not dropped:
        # nothing to add, or the candidate never touched existing code
        return Projection(new, added_names, tuple(dropped), False)

    body = old if old.endswith("\n") else old + "\n"
    if new_import_nodes:
        # insert new imports after the old file's last import (or at top)
        old_lines = body.splitlines(keepends=True)
        last_import_end = 0
        for n in old_tree.body:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                last_import_end = getattr(n, "end_lineno", n.lineno)
        imports_text = "".join(_segment(new, n) if _segment(new, n).endswith("\n") else _segment(new, n) + "\n" for n in new_import_nodes)
        body = "".join(old_lines[:last_import_end]) + imports_text + "".join(old_lines[last_import_end:])
    parts = [body.rstrip("\n") + "\n"]
    for n in additions:
        seg = _segment(new, n)
        parts.append("\n" + seg.rstrip("\n") + "\n")
    projected = "".join(parts)
    try:
        ast.parse(projected)
    except (SyntaxError, ValueError):
        return Projection(new, added_names, tuple(dropped), False)
    return Projection(projected, added_names, tuple(dropped), projected != new)


def _file_entries(candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    files = candidate.get("files")
    if isinstance(files, list) and files:
        return [f for f in files if isinstance(f, dict) and f.get("file_path") and isinstance(f.get("full_content"), str)]
    if candidate.get("file_path") and isinstance(candidate.get("full_content"), str):
        return [candidate]
    return []


@dataclass(frozen=True)
class ProjectionReport:
    candidates: Tuple[Dict[str, Any], ...]
    changed: bool
    notes: Tuple[str, ...]


def project_candidates(candidates: Sequence[Dict[str, Any]], project_root: Path) -> ProjectionReport:
    """Project every test-authoring candidate file that already exists on
    disk. Candidates are copied, never mutated. NEVER raises."""
    if not projection_enabled():
        return ProjectionReport(tuple(candidates), False, ())
    try:
        from backend.core.ouroboros.governance.differential_validation import candidate_is_test_authoring
    except Exception:  # noqa: BLE001
        return ProjectionReport(tuple(candidates), False, ())
    out: List[Dict[str, Any]] = []
    notes: List[str] = []
    changed_any = False
    try:
        for cand in candidates:
            entries = _file_entries(cand)
            pairs = [(str(e["file_path"]), e["full_content"]) for e in entries]
            if not pairs or not candidate_is_test_authoring(pairs):
                out.append(cand)
                continue
            new_cand = dict(cand)
            new_files = []
            cand_changed = False
            for e in entries:
                rel = str(e["file_path"])
                p = Path(rel) if os.path.isabs(rel) else Path(project_root) / rel
                try:
                    old = p.read_text(encoding="utf-8") if p.is_file() else None
                except OSError:
                    old = None
                if old is None:
                    new_files.append(e)
                    continue
                proj = project_append_only(old, e["full_content"])
                if proj.changed:
                    cand_changed = True
                    ne = dict(e)
                    ne["full_content"] = proj.content
                    new_files.append(ne)
                    notes.append(f"{rel}: kept existing code, appended {list(proj.added)}, discarded edits to {list(proj.dropped)}")
                else:
                    new_files.append(e)
            if cand_changed:
                changed_any = True
                if isinstance(cand.get("files"), list) and cand.get("files"):
                    new_cand["files"] = new_files
                else:
                    new_cand["full_content"] = new_files[0]["full_content"]
            out.append(new_cand)
    except Exception:  # noqa: BLE001 — additive, never fatal
        logger.debug("[Orchestrator] append-only projection degraded", exc_info=True)
        return ProjectionReport(tuple(candidates), False, ())
    return ProjectionReport(tuple(out), changed_any, tuple(notes))

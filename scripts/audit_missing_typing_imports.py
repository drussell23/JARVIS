#!/usr/bin/env python3
"""Names used in ANNOTATIONS that nothing binds — proven, not guessed.

`backend/vision/window_relationship_detector.py` used ``Any`` in a dataclass
field annotation without importing it. The module raised NameError at import,
so every test touching it reported a COLLECTION ERROR — zero coverage, not a
red test. A grep sweep suggested ~68 more candidates, but a grep cannot tell
an annotation from a string, a comment, or a locally-bound name.

TWO BUCKETS, AND ONLY ONE IS A BUG
----------------------------------
``from __future__ import annotations`` makes every annotation a STRING at
runtime — the name is never evaluated, so a missing import cannot raise. That
single line is the difference between a crash and lint debt:

  FATAL       missing binding, NO __future__ import  -> NameError on import
  TYPING DEBT missing binding, HAS __future__ import -> inert at runtime

Only FATAL is patched. Rewriting the other bucket would be churn dressed as
a fix, and would edit files whose behaviour is already correct.

SCOPE IS EVALUATED, NOT ASSUMED
-------------------------------
An annotation inside a function BODY is evaluated when the function runs, not
at import, so it cannot break collection. Only module-level and class-level
annotations (dataclass fields, class attributes) and function SIGNATURES —
evaluated at `def` time, i.e. at import — are fatal. The walk tracks which it
is rather than treating every annotation alike.

Read-only unless ``--fix`` is passed. Never imports the code it measures.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

_BUILTINS = set(dir(builtins))


def _bound_names(tree: ast.Module) -> Set[str]:
    """Every name bound at module scope — imports, assignments, defs."""
    out: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            out |= {a.asname or a.name for a in node.names}
        elif isinstance(node, ast.Import):
            out |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, ast.alias):
            out.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            out |= set(node.names)
    return out


def _names_in(expr: ast.AST) -> Set[str]:
    """Bare names an annotation expression references."""
    return {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}


def _eager_annotations(tree: ast.Module) -> List[Tuple[ast.AST, int]]:
    """Annotations EVALUATED AT IMPORT: module/class level, and signatures.

    A body-local annotation is evaluated only when that code runs, so it
    cannot produce the import-time NameError this audit is about.
    """
    found: List[Tuple[ast.AST, int]] = []

    def visit(node: ast.AST, at_import: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AnnAssign) and child.annotation:
                if at_import:
                    found.append((child.annotation, child.lineno))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # The SIGNATURE is evaluated at def time; the BODY is not.
                a = child.args
                for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs,
                            a.vararg, a.kwarg):
                    if arg is not None and arg.annotation is not None:
                        found.append((arg.annotation, arg.lineno))
                if child.returns is not None:
                    found.append((child.returns, child.lineno))
                visit(child, False)
                continue
            visit(child, at_import and not isinstance(child, ast.Lambda))

    visit(tree, True)
    return found


def audit(path: Path) -> Dict[str, object] | None:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return None
    future = any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )
    bound = _bound_names(tree) | _BUILTINS
    missing: Dict[str, int] = {}
    for expr, lineno in _eager_annotations(tree):
        for name in _names_in(expr) - bound:
            missing.setdefault(name, lineno)
    if not missing:
        return None
    return {"path": path, "future": future, "missing": missing}


#: Names this audit can fix by itself. Anything else is REPORTED, never
#: invented — guessing which module a project-local name came from is how an
#: automated fixer introduces a wrong import that type-checks and misbehaves.
_TYPING_NAMES = {
    "Any", "Callable", "ClassVar", "Dict", "FrozenSet", "Iterable", "Iterator",
    "List", "Literal", "Mapping", "NamedTuple", "Optional", "Sequence", "Set",
    "Tuple", "Type", "Union", "Awaitable", "Coroutine", "AsyncIterator",
    "Deque", "DefaultDict", "Generator", "Protocol", "TypeVar", "cast",
}


def apply_fix(path: Path, names: Set[str]) -> bool:
    """Extend an existing ``from typing import`` line, or add one."""
    fixable = sorted(names & _TYPING_NAMES)
    if not fixable or fixable != sorted(names):
        return False                      # partial fixes would mislead
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.startswith("from typing import ") and "(" not in ln:
            have = [x.strip() for x in ln.split("import", 1)[1].split(",")]
            merged = sorted(set(have) | set(fixable))
            lines[i] = f"from typing import {', '.join(merged)}\n"
            path.write_text("".join(lines), encoding="utf-8")
            return True
    for i, ln in enumerate(lines):
        if ln.startswith(("import ", "from ")):
            lines.insert(i, f"from typing import {', '.join(fixable)}\n")
            path.write_text("".join(lines), encoding="utf-8")
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="*", default=["backend"])
    ap.add_argument("--fix", action="store_true")
    args = ap.parse_args()

    fatal, debt = [], []
    for root in args.roots or ["backend"]:
        for p in sorted(Path(root).rglob("*.py")):
            if "venv" in p.parts or "site-packages" in p.parts:
                continue
            r = audit(p)
            if r is None:
                continue
            (debt if r["future"] else fatal).append(r)

    print(f"FATAL (NameError at import): {len(fatal)}")
    for r in fatal:
        print(f"  {r['path']}: {', '.join(sorted(r['missing']))}")
    print(f"\nSTATIC TYPING DEBT (inert — has __future__ annotations): "
          f"{len(debt)}")
    for r in debt[:15]:
        print(f"  {r['path']}: {', '.join(sorted(r['missing']))}")
    if len(debt) > 15:
        print(f"  … and {len(debt) - 15} more")

    if args.fix:
        fixed = sum(1 for r in fatal
                    if apply_fix(r["path"], set(r["missing"])))
        print(f"\npatched {fixed}/{len(fatal)} fatal module(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

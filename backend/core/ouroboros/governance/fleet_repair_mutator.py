"""Fleet repair mutator — anti-overfit variant generation for the battery.

Produces semantically-IDENTICAL variants of a battery defect by consistently
renaming the function + its parameters + locals (and propagating the new
function name into the test). Renaming cannot change behavior, so:

  * the SAME bug is preserved (the test still catches it), and
  * the correct fix is still verifiable by the (renamed) test.

This defeats model memorization of the 8 fixed defects WITHOUT the unsound
defect-synthesis-from-arbitrary-commits approach (you cannot auto-derive ground
truth for arbitrary code). Pure + deterministic given a seed (stdlib only).

The seed MAY be derived from recent-commit topology (see seed_from_text) so the
variant identifiers drift as the repo evolves — honoring "test against the
evolving shape of the repo" via the only sound mechanism: structural variation
of verifiable cases.
"""
from __future__ import annotations

import ast
import hashlib
import re
from typing import Dict

from backend.core.ouroboros.governance.fleet_repair_battery import Defect


def seed_from_text(text: str) -> int:
    """Deterministic non-negative seed from arbitrary text (e.g. concatenated
    recent commit subjects). NEVER raises."""
    try:
        h = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
        return int(h[:8], 16)
    except Exception:  # noqa: BLE001
        return 0


def _token(seed: int, salt: str) -> str:
    """Short deterministic identifier-safe suffix token from (seed, salt)."""
    h = hashlib.sha256(f"{seed}:{salt}".encode("utf-8")).hexdigest()
    return "v" + h[:4]


class _Renamer(ast.NodeTransformer):
    def __init__(self, mapping: Dict[str, str]) -> None:
        self._m = mapping

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name in self._m:
            node.name = self._m[node.name]
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AST) -> ast.AST:
        return self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_arg(self, node: ast.arg) -> ast.AST:
        if node.arg in self._m:
            node.arg = self._m[node.arg]
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self._m:
            node.id = self._m[node.id]
        return node


def _collect_renamable(fn: ast.FunctionDef) -> Dict[str, str]:
    """The function name is renamed by the caller (needed for the test too);
    here we collect param + local-variable names to rename. NEVER raises."""
    names = set()
    for a in list(fn.args.args) + list(fn.args.posonlyargs) + list(fn.args.kwonlyargs):
        names.add(a.arg)
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Assign):
            for t in sub.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return {n: "" for n in names}  # values filled by caller


def mutate(defect: Defect, *, seed: int) -> Defect:
    """Return a renamed, semantically-identical variant of *defect*.
    Falls back to the original defect on any parse/unparse error (fail-soft)."""
    try:
        tree = ast.parse(defect.buggy_src)
        fn = next(
            (n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
            None,
        )
        if fn is None:
            return defect
        new_fn_name = f"{defect.fn_name}_{_token(seed, 'fn')}"
        mapping: Dict[str, str] = {defect.fn_name: new_fn_name}
        for local in _collect_renamable(fn):  # type: ignore[arg-type]
            if local == defect.fn_name:
                continue
            mapping[local] = f"{local}_{_token(seed, local)}"
        new_tree = _Renamer(mapping).visit(tree)
        ast.fix_missing_locations(new_tree)
        new_buggy = ast.unparse(new_tree)
        # Propagate ONLY the function-name rename into the test (the test
        # references the function by name via import + call; it does not see
        # the function's internal params/locals). Whole-word replace.
        new_test = re.sub(
            rf"\b{re.escape(defect.fn_name)}\b", new_fn_name, defect.test_src,
        )
        return Defect(
            name=f"{defect.name}:mut",
            fn_name=new_fn_name,
            buggy_src=new_buggy + "\n",
            test_src=new_test,
        )
    except Exception:  # noqa: BLE001 — fail-soft to the canonical defect
        return defect

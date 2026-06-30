"""Adaptive Hybrid Reverse-Dependency Resolver (Task 5b).

Powers Gate (2) of the Iron Triad -- it answers "every test that transitively
touches the modified code". It is the async ``graph_fn(scope_files) -> Set[str]``
consumed by ``blast_radius_verify.acquire_blast_radius_token``.

Design: an **adaptive hybrid** -- Oracle-first when the index is warm (the
changed files are indexed), AST-fallback when the Oracle is cold, missing, or
erroring. Both paths converge on an "impacted module set", which a shared
``_modules_to_tests`` step maps to repo-relative test-file paths.

Load-bearing guarantees:

* **Cyclic Dependency Armor** -- the transitive reverse closure is ITERATIVE
  (a ``collections.deque`` worklist + a ``visited`` set), never recursive. A
  circular import (A <-> B) terminates via the ``visited`` set; a deep chain
  (thousands deep) never raises ``RecursionError``. A cycle is RESOLVED, never
  treated as a build failure.
* **Fail-closed only on real build failure** -- ``ReverseDepGraphError`` is
  raised ONLY when the graph genuinely cannot be built (unreadable
  ``repo_root``, or no changed file resolves to a module under root). A
  per-file syntax error in an UNRELATED file is skipped, not fatal. An empty
  result (no dependent tests) is a VALID return.
* **Oracle never breaks Gate 2** -- every Oracle interaction is wrapped in
  try/except and falls back to the AST path on any error.

Constraints: Python 3.9 (``asyncio.wait_for`` only, no ``asyncio.timeout``),
ASCII-only, ``from __future__ import annotations``.
"""

from __future__ import annotations

import ast as _ast
import asyncio
import collections
import logging
import os
from pathlib import Path
from typing import Dict, Sequence, Set

logger = logging.getLogger(__name__)


# Default reverse-reachability depth for the Oracle blast-radius call. The AST
# path is unbounded (iterative closure); the Oracle path uses its own BFS, so
# we ask for a generous depth to capture deep transitive impact.
_ORACLE_MAX_DEPTH = 50


# Test directory names -- mirrors test_runner's JARVIS_TEST_DIR_NAMES default.
def _test_dir_names() -> frozenset:
    return frozenset(
        n.strip()
        for n in os.environ.get("JARVIS_TEST_DIR_NAMES", "tests,test").split(",")
        if n.strip()
    )


class ReverseDepGraphError(RuntimeError):
    """Graph could not be built (IO error, unresolvable changed-file module).

    Raised ONLY on a genuine build failure so Gate (2)'s fail-closed guard
    fires. Cycles and empty results never raise.
    """


# ---------------------------------------------------------------------------
# Module <-> path mapping
# ---------------------------------------------------------------------------

def _module_from_relpath(rel_path: str) -> str:
    """Map a repo-relative ``*.py`` path to a dotted module name.

    ``a.py`` -> ``a``; ``tests/test_c.py`` -> ``tests.test_c``;
    ``pkg/__init__.py`` -> ``pkg``. Returns ``""`` for non-``.py`` paths.
    """
    rel = rel_path.replace("\\", "/").strip("/")
    if not rel.endswith(".py"):
        return ""
    rel = rel[: -len(".py")]
    parts = [p for p in rel.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _relpath_under_root(file_path: str, root: str) -> str:
    """Return *file_path* as a path relative to *root*, or ``""`` if it escapes
    root or is not a ``.py`` file. Accepts already-relative paths."""
    fp = file_path.replace("\\", "/")
    if os.path.isabs(fp):
        try:
            rel = os.path.relpath(fp, root)
        except ValueError:
            return ""
    else:
        rel = fp
    rel = rel.replace("\\", "/")
    if rel.startswith("..") or rel.startswith("/"):
        return ""
    if not rel.endswith(".py"):
        return ""
    return rel


def _changed_modules(changed_files: Sequence[str], root: str) -> Set[str]:
    """Dotted modules of the changed files themselves (under root)."""
    mods: Set[str] = set()
    for cf in changed_files:
        rel = _relpath_under_root(cf, root)
        if not rel:
            continue
        mod = _module_from_relpath(rel)
        if mod:
            mods.add(mod)
    return mods


def _is_test_module(module: str, dir_names: frozenset) -> bool:
    """True if *module* names a ``test_*.py`` file under a test directory."""
    parts = module.split(".")
    if not parts:
        return False
    leaf = parts[-1]
    if not leaf.startswith("test_"):
        return False
    return any(p in dir_names for p in parts[:-1]) or (
        # allow a top-level test_*.py only if it lives directly under a test dir
        len(parts) > 1 and parts[0] in dir_names
    )


# ---------------------------------------------------------------------------
# AST forward graph build + inversion + iterative closure
# ---------------------------------------------------------------------------

def _build_forward_import_graph(root: str) -> Dict[str, Set[str]]:
    """AST-parse every ``*.py`` under *root*, mapping
    ``dotted_module -> {dotted module names it imports}``.

    Mirrors ``test_runner._build_test_import_map``: syntactically broken or
    unreadable files are silently skipped (a broken UNRELATED file is not a
    build failure). Wholesale IO failure is the caller's concern (it checks
    ``os.path.isdir`` first).
    """
    graph: Dict[str, Set[str]] = {}
    root_path = Path(root)

    for py_file in root_path.rglob("*.py"):
        if not py_file.is_file():
            continue
        rel = os.path.relpath(str(py_file), root).replace("\\", "/")
        module = _module_from_relpath(rel)
        if not module:
            continue
        is_init = rel == "__init__.py" or rel.endswith("/__init__.py")
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = _ast.parse(source, filename=str(py_file))
        except (SyntaxError, OSError, UnicodeDecodeError, ValueError):
            # Skip unrelated broken/unreadable files -- not a build failure.
            continue

        imports: Set[str] = graph.setdefault(module, set())
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    if alias.name:
                        imports.add(alias.name)
            elif isinstance(node, _ast.ImportFrom):
                if node.level and node.level > 0:
                    # Relative import -- resolve against the importing module's
                    # package using CPython's algorithm, so intra-package edges
                    # (``from . import sib``) stay visible to the reverse graph.
                    _add_relative_import_edges(imports, module, is_init, node)
                else:
                    mod = node.module or ""
                    if mod:
                        imports.add(mod)
                        for alias in node.names:
                            if alias.name:
                                imports.add(f"{mod}.{alias.name}")

    return graph


def _add_relative_import_edges(
    imports: Set[str],
    importing_module: str,
    is_init: bool,
    node: _ast.ImportFrom,
) -> None:
    """Resolve a relative ``ImportFrom`` to absolute dotted target(s) and add
    the corresponding forward edges to *imports*.

    Mirrors CPython's resolution: the importing module's package is the seed,
    ``node.level`` walks upward. A relative import that escapes the tree (more
    levels than the package is deep) is skipped safely -- never raises.
    """
    # Package of the importing module. ``_module_from_relpath`` already
    # collapses ``pkg/__init__.py`` -> ``pkg``, so an __init__ module IS its
    # own package; any other module's package drops its trailing leaf.
    if is_init:
        package = importing_module
    elif "." in importing_module:
        package = importing_module.rsplit(".", 1)[0]
    else:
        package = ""

    pkg_parts = package.split(".") if package else []
    drop = node.level - 1
    if drop > len(pkg_parts):
        # Escapes above the package tree -- not resolvable; skip safely.
        return
    base = ".".join(pkg_parts[: len(pkg_parts) - drop])

    if node.module:
        target_module = f"{base}.{node.module}" if base else node.module
    else:
        target_module = base

    if target_module:
        imports.add(target_module)
    for alias in node.names:
        if not alias.name:
            continue
        if target_module:
            imports.add(f"{target_module}.{alias.name}")
        elif not node.module:
            # Top-level ``from . import x`` -> the top-level module ``x``.
            imports.add(alias.name)


def _invert(graph: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    """Invert ``importer -> {imported}`` into ``imported -> {importers}``."""
    reverse: Dict[str, Set[str]] = {}
    for importer, imported_set in graph.items():
        for imported in imported_set:
            reverse.setdefault(imported, set()).add(importer)
    return reverse


def _transitive_reverse_closure(
    reverse_graph: Dict[str, Set[str]],
    seed_modules: Set[str],
) -> Set[str]:
    """Iterative reverse reachability -- THE cyclic-dependency armor.

    Uses a ``deque`` worklist + a ``visited`` set. A cycle (A <-> B) terminates
    because re-encountered nodes are not re-queued; a deep chain never recurses,
    so no ``RecursionError``. Returns every module reachable by following
    "imported-by" edges from the seeds (seeds excluded from the result).
    """
    visited: Set[str] = set(seed_modules)
    worklist = collections.deque(seed_modules)

    while worklist:
        current = worklist.popleft()
        for importer in reverse_graph.get(current, ()):  # cheap miss -> ()
            if importer not in visited:
                visited.add(importer)
                worklist.append(importer)

    return visited - set(seed_modules)


def _impacted_via_ast(changed_modules: Set[str], root: str) -> Set[str]:
    """Build the forward graph, invert it, and return the transitive reverse
    closure of *changed_modules*. Cycle-armored (iterative)."""
    forward = _build_forward_import_graph(root)
    reverse = _invert(forward)
    return _transitive_reverse_closure(reverse, changed_modules)


# ---------------------------------------------------------------------------
# Oracle path
# ---------------------------------------------------------------------------

def _oracle_can_answer(
    oracle: object,
    changed_files: Sequence[str],
    root: str,
) -> bool:
    """Warmth check: are ALL changed files indexed by the Oracle?

    Returns True only if every changed file resolves to >= 1 node via
    ``find_nodes_in_file``. Any Oracle error -> False (fall back to AST; the
    Oracle can never break Gate 2). An empty ``changed_files`` -> False.
    """
    find_nodes = getattr(oracle, "find_nodes_in_file", None)
    if find_nodes is None:
        return False
    try:
        any_file = False
        for cf in changed_files:
            rel = _relpath_under_root(cf, root)
            if not rel:
                continue
            any_file = True
            nodes = find_nodes(rel)
            if not nodes:
                return False
        return any_file
    except Exception:  # noqa: BLE001 -- Oracle must never break Gate 2
        logger.debug("Oracle warmth check failed; falling back to AST", exc_info=True)
        return False


def _impacted_via_oracle(
    oracle: object,
    changed_files: Sequence[str],
    root: str,
) -> Set[str]:
    """Transitive impacted modules via the Oracle blast radius.

    For each changed file's nodes, union ``directly_affected`` and
    ``transitively_affected`` and map each affected NodeID to its dotted module
    via ``NodeID.file_path``.
    """
    impacted: Set[str] = set()
    for cf in changed_files:
        rel = _relpath_under_root(cf, root)
        if not rel:
            continue
        for node in oracle.find_nodes_in_file(rel):  # type: ignore[attr-defined]
            blast = oracle.compute_blast_radius(  # type: ignore[attr-defined]
                node, max_depth=_ORACLE_MAX_DEPTH
            )
            affected = set(getattr(blast, "directly_affected", set()))
            affected |= set(getattr(blast, "transitively_affected", set()))
            for affected_node in affected:
                node_fp = getattr(affected_node, "file_path", None)
                if not node_fp:
                    continue
                mod = _module_from_relpath(node_fp.replace("\\", "/"))
                if mod:
                    impacted.add(mod)
    return impacted


# ---------------------------------------------------------------------------
# Shared module -> tests mapping
# ---------------------------------------------------------------------------

def _modules_to_tests(modules: Set[str], root: str) -> Set[str]:
    """Select the test files among *modules* and add name-convention matches.

    A module is a test if it names ``test_*.py`` under a test directory. Plus
    name-convention resolution: a changed/impacted ``foo`` whose sibling
    ``test_foo.py`` (or ``tests/test_foo.py``) exists on disk is included.
    Returns repo-relative test paths.
    """
    dir_names = _test_dir_names()
    tests: Set[str] = set()

    for module in modules:
        if _is_test_module(module, dir_names):
            tests.add(module.replace(".", "/") + ".py")
            continue
        # Name-convention: foo -> test_foo.py (sibling) or tests/test_foo.py.
        parts = module.split(".")
        leaf = parts[-1]
        prefix = parts[:-1]
        candidates = []
        sibling = "/".join(prefix + [f"test_{leaf}.py"])
        candidates.append(sibling)
        for tdn in sorted(dir_names):
            candidates.append(f"{tdn}/test_{leaf}.py")
        for cand in candidates:
            if (Path(root) / cand).is_file():
                tests.add(cand)

    return tests


# ---------------------------------------------------------------------------
# Public facade
# ---------------------------------------------------------------------------

async def resolve_reverse_dependency_tests(
    changed_files: Sequence[str],
    *,
    repo_root: str,
    oracle: object | None = None,
) -> Set[str]:
    """Resolve the set of repo-relative test files that transitively touch
    *changed_files*.

    Adaptive hybrid: Oracle-first when warm, AST-fallback otherwise. Raises
    ``ReverseDepGraphError`` only on a genuine build failure (unreadable
    ``repo_root``, or no changed file resolves to a module under root). An empty
    result is a VALID return.
    """
    root = os.path.abspath(repo_root)

    # Fail-closed: repo_root must be a readable directory.
    if not os.path.isdir(root):
        raise ReverseDepGraphError(
            f"repo_root is not a readable directory: {repo_root!r}"
        )

    # Fail-closed: at least one changed file must resolve to a module under root.
    # Distinguish a config-only changeset (no ``.py`` at all -> nothing Python to
    # analyze -> EMPTY result is correct) from a genuine build problem (``.py``
    # present but unresolvable under root -> fail-closed raise). UNDER-inclusion
    # of a real dependent test is the dangerous direction, so only the latter
    # trips Gate (2)'s fail-closed guard.
    changed_modules = _changed_modules(changed_files, root)
    if not changed_modules:
        has_py = any(
            cf.replace("\\", "/").endswith(".py") for cf in changed_files
        )
        if not has_py:
            # Config-only changeset (.yaml/.md/...): no Python changed -> no
            # dependent tests. Empty set is the correct, valid answer.
            return set()
        raise ReverseDepGraphError(
            "no changed file resolves to a module under repo_root "
            f"(changed_files={list(changed_files)!r})"
        )

    impacted: Set[str]

    # Oracle-first (warm) -- guarded so the Oracle can never break Gate 2.
    if oracle is not None and _oracle_can_answer(oracle, changed_files, root):
        try:
            impacted = _impacted_via_oracle(oracle, changed_files, root)
        except Exception:  # noqa: BLE001 -- fall back to AST on any Oracle error
            logger.debug(
                "Oracle blast-radius failed; falling back to AST", exc_info=True
            )
            impacted = await _impacted_via_ast_async(changed_modules, root)
    else:
        impacted = await _impacted_via_ast_async(changed_modules, root)

    all_modules = impacted | changed_modules
    return _modules_to_tests(all_modules, root)


async def _impacted_via_ast_async(changed_modules: Set[str], root: str) -> Set[str]:
    """Run the heavy AST build off the event loop via run_in_executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _impacted_via_ast, changed_modules, root
    )

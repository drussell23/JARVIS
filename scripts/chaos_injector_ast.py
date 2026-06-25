#!/usr/bin/env python3
"""chaos_injector_ast.py -- Dynamic AST Chaos Injector (A1 Live-Fire Chaos Harness).

PURPOSE
-------
An EXTERNAL saboteur that sits OUTSIDE the O+V architecture. It autonomously
picks a PURE LEAF FUNCTION (no I/O, no side effects, operates on its params and
returns a computed value) that has a CONFIRMED GREEN unit test, then injects ONE
real, minimal, test-detectable bug (flip a binary operator, or alter a numeric /
string return literal). It then RUNS the corresponding test and CONFIRMS it now
FAILS -- proving the injected bug is genuinely detectable. This is the live-fire
input for proving O+V's sensors self-detect + self-repair a regression.

The whole point: emit a bug that turns a green test RED, so O+V has something
real to detect. If the mutation is inert (test still green), revert and try a
different function / mutation.

DISCIPLINE (load-bearing)
-------------------------
* Fully DYNAMIC -- no hardcoded target. Target acquisition is AST-based.
* CONSERVATIVE purity analysis -- prefer false-negatives (skip anything
  ambiguous). A function is only viable if it is structurally provably pure.
* DENYLIST IS ABSOLUTE -- the governance safety cage, the harness/scripts, the
  tests dirs, __init__.py and migrations are NEVER selected.
* MANIFEST BEFORE MUTATE -- the original source is recorded in the manifest
  before a single byte is mutated, so revert is ALWAYS possible.
* GREEN-TEST REQUIRED -- a function is only a viable target if a test exercises
  it and that test passes pre-injection.
* Standalone -- never imports the O+V cage. $0 / pure local.
* ASCII only. ``from __future__ import annotations``. Python 3.9+.

CLI
---
    python3 scripts/chaos_injector_ast.py --inject [--seed N] [--now ISO] [--force]
    python3 scripts/chaos_injector_ast.py --revert
    python3 scripts/chaos_injector_ast.py --status
    python3 scripts/chaos_injector_ast.py --dry-run [--seed N]    # ZERO writes
    python3 scripts/chaos_injector_ast.py --list-candidates [--seed N]
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

# Standalone-invocation bootstrap: ensure the repo root (parent of scripts/) is
# on sys.path. We DELIBERATELY do not import anything from backend/* -- this is
# an external saboteur and must stay decoupled from the O+V cage.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

INJECTOR_VERSION = "1.0.0"
MANIFEST_REL_PATH = os.path.join(".jarvis", "chaos_manifest.json")


def _log(msg: str) -> None:
    """Loud structured logging."""
    print(f"[ChaosInjector] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Denylist -- ABSOLUTE. These path fragments are never selected, period.
# --------------------------------------------------------------------------- #

# Safety-cage module basenames (the O+V governance cage). Matched anywhere in
# the path. Mutating any of these would be catastrophic.
_SAFETY_CAGE_NAMES: Tuple[str, ...] = (
    "semantic_firewall",
    "iron_gate",
    "risk_tier",
    "scoped_tool",        # scoped_tool_backend, scoped_tool*
    "critical_elevation",
    "swarm_sentinel",
    "agent_message_bus",
)

# Path-fragment denylist (os.sep-normalized). Any candidate whose normalized
# path contains one of these fragments is rejected outright.
_DENY_PATH_FRAGMENTS: Tuple[str, ...] = (
    os.sep + "governance" + os.sep,   # the safety cage dir
    os.sep + "scripts" + os.sep,      # the harness/scripts themselves
    os.sep + "tests" + os.sep,        # tests dirs
    os.sep + "test" + os.sep,
    os.sep + "migrations" + os.sep,
    os.sep + "node_modules" + os.sep,
    os.sep + "__pycache__" + os.sep,
    os.sep + ".git" + os.sep,
    os.sep + "venv" + os.sep,
    os.sep + ".venv" + os.sep,
)


def _is_denied(path: str) -> bool:
    """ABSOLUTE denylist check. Conservative: deny on any match."""
    norm = os.sep + os.path.normpath(path).strip(os.sep) + os.sep
    base = os.path.basename(path)
    if base == "__init__.py":
        return True
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    for frag in _DENY_PATH_FRAGMENTS:
        if frag in norm:
            return True
    low = norm.lower()
    for cage in _SAFETY_CAGE_NAMES:
        if cage in low:
            return True
    return False


# --------------------------------------------------------------------------- #
# Target directory discovery (dynamic, conservative SAFE allowlist).
# --------------------------------------------------------------------------- #

# Directory-name AND file-name tokens that mark a SAFE pure-util location. A
# directory (or a .py file's basename) is a candidate iff it matches one of
# these (case-insensitive contains). Conservative pure-util surface.
_SAFE_DIR_TOKENS: Tuple[str, ...] = (
    "util", "utils", "helpers", "_math", "math_", "format", "schema",
    "classif", "predicate", "compute", "topology", "render", "polish",
)


def _default_target_dirs(repo_root: str) -> List[str]:
    """Discover the default SAFE allowlist of pure-util dirs under backend/.

    Conservative: only directories whose basename matches a SAFE token, and that
    are NOT themselves denied. Env ``JARVIS_CHAOS_TARGET_DIRS`` (os.pathsep list)
    overrides this entirely.
    """
    env = (os.environ.get("JARVIS_CHAOS_TARGET_DIRS", "") or "").strip()
    if env:
        out: List[str] = []
        for raw in env.split(os.pathsep):
            raw = raw.strip()
            if not raw:
                continue
            cand = raw if os.path.isabs(raw) else os.path.join(repo_root, raw)
            if os.path.isdir(cand):
                out.append(cand)
        return out

    backend = os.path.join(repo_root, "backend")
    found: List[str] = []
    if not os.path.isdir(backend):
        return found
    for dirpath, dirnames, _files in os.walk(backend):
        # Prune obviously-bad subtrees early.
        dirnames[:] = [
            d for d in dirnames
            if d not in ("__pycache__", "tests", "test", "migrations", "node_modules")
        ]
        base = os.path.basename(dirpath).lower()
        if any(tok in base for tok in _SAFE_DIR_TOKENS):
            if not _is_denied(dirpath):
                found.append(dirpath)
    found.sort()
    return found


def _default_target_files(repo_root: str) -> List[str]:
    """Discover individual SAFE-named .py files under backend/ whose basename
    matches a safe pure-util token (e.g. ``*_utils.py``, ``*format*.py``,
    ``telemetry_schemas.py``). Complements the dir-based allowlist so the
    injector surfaces real targets even when pure utils do not live in a folder
    literally named ``utils/``. Still subject to the ABSOLUTE denylist.

    Skipped entirely when ``JARVIS_CHAOS_TARGET_DIRS`` is set (explicit override
    means the operator chose the surface).
    """
    if (os.environ.get("JARVIS_CHAOS_TARGET_DIRS", "") or "").strip():
        return []
    backend = os.path.join(repo_root, "backend")
    out: List[str] = []
    if not os.path.isdir(backend):
        return out
    for dirpath, dirnames, files in os.walk(backend):
        dirnames[:] = [
            d for d in dirnames
            if d not in ("__pycache__", "tests", "test", "migrations", "node_modules")
        ]
        for f in files:
            if not f.endswith(".py"):
                continue
            low = f.lower()
            if any(tok in low for tok in _SAFE_DIR_TOKENS):
                p = os.path.join(dirpath, f)
                if not _is_denied(p):
                    out.append(p)
    out.sort()
    return out


# --------------------------------------------------------------------------- #
# AST purity analysis -- CONSERVATIVE (fail-safe = skip).
# --------------------------------------------------------------------------- #

# ALLOWLIST of pure, side-effect-free builtins that a pure leaf may CALL by bare
# name. Anything not in this set (calling an unknown free name) is rejected --
# conservative default-deny so we never mutate a function that secretly does I/O
# through a helper. (Intentionally excludes print/open/input/exec/eval/etc.)
_PURE_BUILTIN_CALLS: frozenset = frozenset({
    "abs", "min", "max", "sum", "len", "round", "pow", "divmod",
    "int", "float", "str", "bool", "bytes", "complex",
    "list", "tuple", "set", "frozenset", "dict",
    "sorted", "reversed", "enumerate", "zip", "map", "filter", "range",
    "all", "any", "ord", "chr", "hex", "oct", "bin", "repr", "format",
    "isinstance", "issubclass", "type", "hash", "id",
})

# Attribute roots whose use signals I/O / global state / network. Retained as a
# secondary belt; the primary defence is the allowlist + the
# method-call-on-non-literal rejection below.
_IMPURE_ATTR_ROOTS: frozenset = frozenset({
    "os", "sys", "subprocess", "socket", "requests", "logging", "log",
    "logger", "open", "shutil", "pathlib", "Path", "io", "tempfile",
    "asyncio", "threading", "multiprocessing", "random", "time", "datetime",
    "urllib", "http", "json", "pickle", "sqlite3", "aiohttp", "httpx",
    "self", "cls",  # method on object => not a pure leaf
})

# AST literal node types whose methods are provably pure (e.g. "x".upper(),
# (1, 2).count(3)). A method call on one of these is allowed.
_PURE_LITERAL_NODES = (
    ast.Constant, ast.Str if hasattr(ast, "Str") else ast.Constant,
    ast.List, ast.Tuple, ast.Set, ast.Dict,
    ast.ListComp, ast.SetComp, ast.DictComp,
)


class _PurityVisitor(ast.NodeVisitor):
    """Walks a function body and proves (conservatively) that it is a pure leaf.

    Sets ``self.pure = False`` on ANY disallowed construct. Default-deny: the
    function is pure only if nothing flips the flag.
    """

    def __init__(self, arg_names: Sequence[str]) -> None:
        self.pure = True
        self.reason = ""
        self._args = set(arg_names)

    def _reject(self, reason: str) -> None:
        if self.pure:
            self.pure = False
            self.reason = reason

    # --- disallowed statements -------------------------------------------- #
    def visit_Global(self, node: ast.Global) -> None:
        self._reject("uses global")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self._reject("uses nonlocal")

    def visit_Await(self, node: ast.Await) -> None:
        self._reject("uses await")

    def visit_Yield(self, node: ast.Yield) -> None:
        self._reject("is a generator (yield)")

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self._reject("is a generator (yield from)")

    def visit_With(self, node: ast.With) -> None:
        self._reject("uses with (context manager => likely I/O)")

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._reject("uses async with")

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._reject("uses async for")

    def visit_Import(self, node: ast.Import) -> None:
        self._reject("imports inside body")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._reject("imports inside body")

    def visit_Nested(self) -> None:  # pragma: no cover - helper, not a visitor
        pass

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # A nested function/class makes purity analysis ambiguous -> reject.
        self._reject("contains a nested function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._reject("contains a nested async function")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._reject("contains a nested class")

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # Lambdas are fine in principle, but their body can hide impurity and we
        # do not recurse arg scoping precisely -> conservative reject.
        self._reject("contains a lambda")

    # --- attribute mutation on args / impure roots ------------------------ #
    def visit_Attribute(self, node: ast.Attribute) -> None:
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id in _IMPURE_ATTR_ROOTS:
            self._reject(f"references impure attribute root '{root.id}'")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # Mutating an attribute of an argument (e.g. arg.x = ...) is a side
        # effect on caller state -> reject.
        for tgt in node.targets:
            if isinstance(tgt, ast.Attribute):
                self._reject("assigns to an attribute (mutates object state)")
            if isinstance(tgt, ast.Subscript):
                # arg[k] = v mutates a passed-in container -> reject.
                self._reject("assigns to a subscript (mutates container)")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, (ast.Attribute, ast.Subscript)):
            self._reject("aug-assigns to attribute/subscript (mutation)")
        self.generic_visit(node)

    # --- calls: allowlist-based default-deny ------------------------------ #
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            # Bare-name call: only a known-pure builtin is allowed. An unknown
            # free name could be a helper that does I/O -> reject.
            if func.id not in _PURE_BUILTIN_CALLS:
                self._reject(f"calls non-allowlisted name '{func.id}'")
        elif isinstance(func, ast.Attribute):
            # Method call x.method(...). Allowed ONLY when the receiver is a
            # provably-pure literal (e.g. "s".upper(), [].copy()). A method on a
            # Name (param/local/module) is rejected -- it could be path.unlink(),
            # logger.info(), conn.send(), etc.
            recv = func.value
            if not isinstance(recv, _PURE_LITERAL_NODES):
                label = recv.id if isinstance(recv, ast.Name) else type(recv).__name__
                self._reject(f"calls method on non-literal receiver '{label}'")
        else:
            # Call of a call / subscript / lambda result -> too opaque, reject.
            self._reject("calls an opaque (non-name, non-literal) target")
        self.generic_visit(node)


@dataclass
class _FuncInfo:
    name: str
    node: ast.FunctionDef
    lineno: int
    end_lineno: int
    pure: bool
    reason: str


def _decorator_is_safe(node: ast.FunctionDef) -> bool:
    """Reject any decorator we cannot prove stateless. Allow only a tiny
    allowlist of provably-stateless decorators."""
    safe = {"staticmethod"}  # property/classmethod/lru_cache etc => reject
    for dec in node.decorator_list:
        name = None
        if isinstance(dec, ast.Name):
            name = dec.id
        elif isinstance(dec, ast.Attribute):
            name = dec.attr
        elif isinstance(dec, ast.Call):
            inner = dec.func
            name = inner.id if isinstance(inner, ast.Name) else getattr(inner, "attr", None)
        if name not in safe:
            return False
    return True


def _analyze_function(node: ast.FunctionDef) -> _FuncInfo:
    """Conservatively decide whether a top-level function is a pure leaf."""
    name = node.name
    end = getattr(node, "end_lineno", node.lineno)

    def info(pure: bool, reason: str) -> _FuncInfo:
        return _FuncInfo(name, node, node.lineno, end, pure, reason)

    if name.startswith("__") and name.endswith("__"):
        return info(False, "dunder")
    if not _decorator_is_safe(node):
        return info(False, "has a non-stateless decorator")
    # Reject *args / **kwargs ambiguity? They are fine for purity; keep them.
    arg_names = [a.arg for a in node.args.args]
    if not node.body:
        return info(False, "empty body")
    # Must actually return a computed value (have at least one Return with value).
    has_return_value = any(
        isinstance(n, ast.Return) and n.value is not None
        for n in ast.walk(node)
    )
    if not has_return_value:
        return info(False, "no value-returning return statement")

    visitor = _PurityVisitor(arg_names)
    for stmt in node.body:
        visitor.visit(stmt)
        if not visitor.pure:
            break
    return info(visitor.pure, visitor.reason or "pure leaf")


# --------------------------------------------------------------------------- #
# Candidate model.
# --------------------------------------------------------------------------- #

@dataclass
class Mutation:
    kind: str                 # e.g. "binop:Add->Sub" or "return-literal:int+1"
    lineno: int               # 1-based source line of the mutated token
    col_offset: int
    end_col_offset: int
    original_segment: str     # the exact substring replaced
    mutated_segment: str      # what it became


@dataclass
class Candidate:
    target_file: str          # absolute path
    function: str
    lineno: int
    end_lineno: int
    test_node: str            # PRIMARY pytest node id, e.g. tests/foo.py::test_bar
    # All plausible test nodes exercising this function. Because the planner
    # picks ONE deterministic mutation site per function, a mutation may be
    # inert against one assertion but RED against another -- so injection tries
    # every node until one goes red before abandoning the candidate.
    test_nodes: List[str] = field(default_factory=list)
    # Filled at injection time:
    planned_mutation: Optional[Mutation] = field(default=None)


# --------------------------------------------------------------------------- #
# Test discovery -- find a test that exercises the function, confirm it's green.
# --------------------------------------------------------------------------- #

def _find_test_files(repo_root: str) -> List[str]:
    out: List[str] = []
    tests_root = os.path.join(repo_root, "tests")
    if not os.path.isdir(tests_root):
        return out
    for dirpath, dirnames, files in os.walk(tests_root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for f in files:
            if f.startswith("test_") and f.endswith(".py"):
                out.append(os.path.join(dirpath, f))
    return out


def _test_nodes_for_function(
    repo_root: str, abs_file: str, func_name: str, test_files: Sequence[str],
) -> List[str]:
    """Find pytest node ids of test functions that reference ``func_name`` and
    import the candidate's module. Returns rel-path::testname node ids."""
    module_basename = os.path.splitext(os.path.basename(abs_file))[0]
    nodes: List[str] = []
    for tf in test_files:
        try:
            with open(tf, "r", encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        # Cheap pre-filter: the test must mention both the function name and the
        # module (import path or basename) to be a plausible exerciser.
        if func_name not in src:
            continue
        if module_basename not in src:
            continue
        try:
            tree = ast.parse(src, filename=tf)
        except SyntaxError:
            continue
        rel = os.path.relpath(tf, repo_root)
        for node in tree.body:
            funcs: List[ast.FunctionDef] = []
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                funcs.append(node)
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for sub in node.body:
                    if isinstance(sub, ast.FunctionDef) and sub.name.startswith("test"):
                        funcs.append(sub)
            for fn in funcs:
                # Does this test body reference the function name?
                seg = ast.get_source_segment(src, fn) or ""
                if func_name in seg:
                    if isinstance(node, ast.ClassDef):
                        nodes.append(f"{rel}::{node.name}::{fn.name}")
                    else:
                        nodes.append(f"{rel}::{fn.name}")
    return nodes


def _run_pytest_node(repo_root: str, node_id: str, timeout_s: float) -> Optional[bool]:
    """Run a single pytest node. Returns True (passed), False (failed), or None
    (could not determine -- error / timeout / collection failure)."""
    cmd = [
        sys.executable, "-m", "pytest", node_id,
        "-q", "-p", "no:cacheprovider", "--no-header", "-x",
        "-o", "addopts=", "--rootdir", repo_root,
    ]
    # Hermetic child env: prepend the target repo to PYTHONPATH so the mutated
    # module under test resolves to THIS repo (not a same-named package leaked
    # in from a parent process's sys.path), and strip inherited pytest config
    # that would otherwise drag in a foreign rootdir / addopts.
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    # Never reuse a stale .pyc: a mutate-within-the-same-second can leave an old
    # bytecode cache whose mtime check passes, so the child would import the
    # pre-mutation module and falsely report the test still green.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = repo_root + (os.pathsep + existing_pp if existing_pp else "")
    try:
        proc = subprocess.run(
            cmd, cwd=repo_root, capture_output=True, text=True,
            timeout=timeout_s, env=env,
        )
    except subprocess.TimeoutExpired:
        _log(f"pytest TIMEOUT on {node_id}")
        return None
    except Exception as exc:  # pragma: no cover - defensive
        _log(f"pytest ERROR on {node_id}: {exc!r}")
        return None
    rc = proc.returncode
    if rc == 0:
        return True
    # rc==1 is test failures; treat real failures as False. Other codes
    # (collection error rc==2..5) -> indeterminate.
    if rc == 1:
        return False
    tail = (proc.stdout or "")[-400:] + (proc.stderr or "")[-400:]
    _log(f"pytest node {node_id} rc={rc} (indeterminate). tail: {tail.strip()[:200]}")
    return None


# --------------------------------------------------------------------------- #
# Candidate acquisition (the autonomous, dynamic, AST-based target picker).
# --------------------------------------------------------------------------- #

def _scan_one_file(
    repo_root: str, abs_file: str, test_files: Sequence[str],
) -> List[Candidate]:
    """Scan a single .py file for pure-leaf functions with a plausible test."""
    out: List[Candidate] = []
    if _is_denied(abs_file):
        return out
    try:
        with open(abs_file, "r", encoding="utf-8") as fh:
            src = fh.read()
    except (OSError, UnicodeDecodeError):
        return out
    try:
        tree = ast.parse(src, filename=abs_file)
    except SyntaxError:
        return out
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        fi = _analyze_function(node)
        if not fi.pure:
            continue
        nodes = _test_nodes_for_function(repo_root, abs_file, fi.name, test_files)
        if not nodes:
            continue
        nodes = sorted(set(nodes))
        # ONE candidate per (file, function); carry all its plausible test nodes.
        out.append(Candidate(
            target_file=abs_file,
            function=fi.name,
            lineno=fi.lineno,
            end_lineno=fi.end_lineno,
            test_node=nodes[0],
            test_nodes=nodes,
        ))
    return out


def _scan_pure_leaf_functions(
    repo_root: str,
    target_dirs: Sequence[str],
    extra_files: Optional[Sequence[str]] = None,
) -> List[Candidate]:
    """Find all pure-leaf functions (purity-confirmed) in the target dirs and the
    extra SAFE-named files that have at least one plausible test node. Does NOT
    run the tests."""
    test_files = _find_test_files(repo_root)
    candidates: List[Candidate] = []
    seen_files = set()

    def _consider(abs_file: str) -> None:
        if abs_file in seen_files:
            return
        seen_files.add(abs_file)
        candidates.extend(_scan_one_file(repo_root, abs_file, test_files))

    for tdir in target_dirs:
        for dirpath, dirnames, files in os.walk(tdir):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for f in sorted(files):
                if f.endswith(".py"):
                    _consider(os.path.join(dirpath, f))
    for ef in (extra_files or ()):
        _consider(ef)

    # Deterministic ordering: by file, function, then test node.
    candidates.sort(key=lambda c: (c.target_file, c.function, c.test_node))
    return candidates


# --------------------------------------------------------------------------- #
# Mutation planning -- pick ONE minimal bug from the function's AST.
# --------------------------------------------------------------------------- #

# Binary operator flips (commutative-ish, all genuinely behaviour-changing).
_BINOP_FLIP = {
    ast.Add: ("Add", "Sub", "+", "-"),
    ast.Sub: ("Sub", "Add", "-", "+"),
    ast.Mult: ("Mult", "Div", "*", "/"),
    ast.Div: ("Div", "Mult", "/", "*"),
}
_CMP_FLIP = {
    ast.Lt: ("Lt", "LtE", "<", "<="),
    ast.LtE: ("LtE", "Lt", "<=", "<"),
    ast.Gt: ("Gt", "GtE", ">", ">="),
    ast.GtE: ("GtE", "Gt", ">=", ">"),
    ast.Eq: ("Eq", "NotEq", "==", "!="),
    ast.NotEq: ("NotEq", "Eq", "!=", "=="),
}
_BOOLOP_FLIP = {
    ast.And: ("And", "Or", "and", "or"),
    ast.Or: ("Or", "And", "or", "and"),
}


def _line_col_to_segment_edit(
    lines: List[str], lineno: int, col: int, end_col: int,
    old_text: str, new_text: str,
) -> Optional[Mutation]:
    """Build a Mutation for a single-line token replacement, verifying that the
    source at the given position actually equals ``old_text``."""
    if lineno < 1 or lineno > len(lines):
        return None
    line = lines[lineno - 1]
    if end_col > len(line):
        return None
    actual = line[col:end_col]
    if actual != old_text:
        return None
    return Mutation(
        kind="",  # filled by caller
        lineno=lineno,
        col_offset=col,
        end_col_offset=end_col,
        original_segment=old_text,
        mutated_segment=new_text,
    )


def _iter_mutations(src: str, func: ast.FunctionDef) -> List[Mutation]:
    """Enumerate ALL viable minimal mutation sites for the function in a fixed,
    deterministic scan order: comparison-op flips, then arithmetic-op flips,
    then boolean-op flips, then return-literal alterations. The caller tries
    them in order until one turns a green test red (some sites may be inert
    against a given assertion)."""
    lines = src.splitlines(keepends=False)
    muts: List[Mutation] = []
    seen = set()

    def _push(m: Optional[Mutation]) -> None:
        if m is None:
            return
        key = (m.lineno, m.col_offset, m.end_col_offset, m.mutated_segment)
        if key in seen:
            return
        seen.add(key)
        muts.append(m)

    # --- 1) Comparison operator flip (single-op compares only) ------------- #
    for node in ast.walk(func):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            flip = _CMP_FLIP.get(type(node.ops[0]))
            if not flip:
                continue
            _from, _to, old_tok, new_tok = flip
            left, right = node.left, node.comparators[0]
            seg = _locate_op_token(
                lines, left.end_lineno, left.end_col_offset,
                right.lineno, right.col_offset, old_tok, new_tok,
            )
            if seg:
                seg.kind = f"cmpop:{_from}->{_to}"
                _push(seg)

    # --- 2) Binary arithmetic operator flip -------------------------------- #
    for node in ast.walk(func):
        if isinstance(node, ast.BinOp):
            flip = _BINOP_FLIP.get(type(node.op))
            if not flip:
                continue
            _from, _to, old_tok, new_tok = flip
            left, right = node.left, node.right
            seg = _locate_op_token(
                lines, left.end_lineno, left.end_col_offset,
                right.lineno, right.col_offset, old_tok, new_tok,
            )
            if seg:
                seg.kind = f"binop:{_from}->{_to}"
                _push(seg)

    # --- 3) Boolean operator flip (and<->or) ------------------------------- #
    for node in ast.walk(func):
        if isinstance(node, ast.BoolOp) and len(node.values) >= 2:
            flip = _BOOLOP_FLIP.get(type(node.op))
            if not flip:
                continue
            _from, _to, old_tok, new_tok = flip
            a, b = node.values[0], node.values[1]
            seg = _locate_op_token(
                lines, a.end_lineno, a.end_col_offset,
                b.lineno, b.col_offset, old_tok, new_tok,
            )
            if seg:
                seg.kind = f"boolop:{_from}->{_to}"
                _push(seg)

    # --- 4) Return-literal alteration -------------------------------------- #
    for node in ast.walk(func):
        if not (isinstance(node, ast.Return) and node.value is not None):
            continue
        if isinstance(node.value, ast.Constant):
            _push(_alter_constant(lines, node.value))
    return muts


def _plan_mutation(src: str, func: ast.FunctionDef) -> Optional[Mutation]:
    """Return the FIRST viable mutation (the deterministic planner head). Used by
    --dry-run and the mutation primitive tests."""
    muts = _iter_mutations(src, func)
    return muts[0] if muts else None


def _locate_op_token(
    lines: List[str], start_line: int, start_col: int,
    end_line: int, end_col: int, old_tok: str, new_tok: str,
) -> Optional[Mutation]:
    """Find ``old_tok`` in the source span between two AST nodes (the operator
    region) and build a precise Mutation. Only single-line spans handled (the
    operator is virtually always on the same line as one of the operands)."""
    if start_line != end_line:
        # Multi-line operator region: search only the start line's tail.
        end_line = start_line
        end_col = len(lines[start_line - 1]) if start_line <= len(lines) else start_col
    if start_line < 1 or start_line > len(lines):
        return None
    line = lines[start_line - 1]
    region = line[start_col:end_col]
    idx = region.find(old_tok)
    if idx < 0:
        return None
    # For multi-char tokens like '==' ensure we don't match a substring of a
    # longer operator. Simplest robust check: the located token must be exactly
    # old_tok bounded by non-operator chars OR we matched the first occurrence
    # which for our flip set is safe because we search the operator gap only.
    abs_col = start_col + idx
    return _line_col_to_segment_edit(
        lines, start_line, abs_col, abs_col + len(old_tok), old_tok, new_tok,
    )


def _alter_constant(lines: List[str], val: ast.Constant) -> Optional[Mutation]:
    """Alter a numeric/bool/str return constant minimally and detectably."""
    lineno = val.lineno
    col = val.col_offset
    end_col = getattr(val, "end_col_offset", None)
    if end_col is None or lineno < 1 or lineno > len(lines):
        return None
    line = lines[lineno - 1]
    seg = line[col:end_col]
    v = val.value
    if isinstance(v, bool):
        new = "False" if v else "True"
        mut = _line_col_to_segment_edit(lines, lineno, col, end_col, seg, new)
        if mut:
            mut.kind = f"return-literal:bool->{new}"
        return mut
    if isinstance(v, int):
        # return N -> return (N + 1)  -- wrap to keep it a single token edit.
        new = f"({seg} + 1)"
        mut = _line_col_to_segment_edit(lines, lineno, col, end_col, seg, new)
        if mut:
            mut.kind = "return-literal:int+1"
        return mut
    if isinstance(v, float):
        new = f"({seg} + 1.0)"
        mut = _line_col_to_segment_edit(lines, lineno, col, end_col, seg, new)
        if mut:
            mut.kind = "return-literal:float+1"
        return mut
    if isinstance(v, str):
        # Append a marker char inside the quotes is fragile; instead concat.
        new = f"({seg} + '_chaos')"
        mut = _line_col_to_segment_edit(lines, lineno, col, end_col, seg, new)
        if mut:
            mut.kind = "return-literal:str-append"
        return mut
    return None


def _apply_mutation(src: str, mut: Mutation) -> str:
    """Apply a single-line Mutation to source text. Returns the mutated source.
    Verifies the original segment matches before replacing."""
    lines = src.splitlines(keepends=True)
    if mut.lineno < 1 or mut.lineno > len(lines):
        raise ValueError("mutation line out of range")
    raw = lines[mut.lineno - 1]
    # Work on the line without its trailing newline, then re-attach.
    nl = ""
    body = raw
    if body.endswith("\r\n"):
        nl, body = "\r\n", body[:-2]
    elif body.endswith("\n"):
        nl, body = "\n", body[:-1]
    actual = body[mut.col_offset:mut.end_col_offset]
    if actual != mut.original_segment:
        raise ValueError(
            f"mutation site drift: expected {mut.original_segment!r} got {actual!r}"
        )
    new_body = body[:mut.col_offset] + mut.mutated_segment + body[mut.end_col_offset:]
    lines[mut.lineno - 1] = new_body + nl
    return "".join(lines)


# --------------------------------------------------------------------------- #
# Manifest.
# --------------------------------------------------------------------------- #

def _bump_mtime(path: str) -> None:
    """Push the file's mtime ~2s into the future so a stale .pyc (compiled from
    the pre-mutation source within the same wall-clock second) is rejected by
    the import machinery's source-mtime check. Also remove a sibling .pyc if a
    legacy __pycache__ holds one for this exact module."""
    try:
        import time as _t
        future = _t.time() + 2.0
        os.utime(path, (future, future))
    except OSError:
        pass
    # Best-effort: nuke any cached bytecode for this module.
    try:
        cache_dir = os.path.join(os.path.dirname(path), "__pycache__")
        base = os.path.splitext(os.path.basename(path))[0]
        if os.path.isdir(cache_dir):
            for fn in os.listdir(cache_dir):
                if fn.startswith(base + ".") and fn.endswith(".pyc"):
                    try:
                        os.remove(os.path.join(cache_dir, fn))
                    except OSError:
                        pass
    except OSError:
        pass


def _manifest_path(repo_root: str) -> str:
    return os.path.join(repo_root, MANIFEST_REL_PATH)


def _write_manifest(repo_root: str, data: dict) -> None:
    path = _manifest_path(repo_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _read_manifest(repo_root: str) -> Optional[dict]:
    path = _manifest_path(repo_root)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _delete_manifest(repo_root: str) -> None:
    path = _manifest_path(repo_root)
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Core operations.
# --------------------------------------------------------------------------- #

@dataclass
class InjectConfig:
    repo_root: str
    seed: int = 0
    now_iso: str = ""
    force: bool = False
    test_timeout_s: float = 60.0
    verify_green: bool = True   # run the test pre-injection to confirm green
    max_attempts: int = 25
    # Chaos Readiness Handshake: optional readiness surface to probe BEFORE
    # mutating. Empty -> handshake skipped (standalone / dry-run / unit-test
    # usage). Env fallbacks: JARVIS_CHAOS_READINESS_URL / _LOG.
    readiness_url: str = ""
    readiness_log: str = ""


def _select_function_node(src: str, func_name: str, lineno: int) -> Optional[ast.FunctionDef]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name and node.lineno == lineno:
            return node
    # Fallback: match by name only.
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    return None


def acquire_candidates(cfg: InjectConfig) -> List[Candidate]:
    target_dirs = _default_target_dirs(cfg.repo_root)
    extra_files = _default_target_files(cfg.repo_root)
    return _scan_pure_leaf_functions(cfg.repo_root, target_dirs, extra_files)


def _seed_order(candidates: List[Candidate], seed: int) -> List[Candidate]:
    """Deterministically rotate candidate order by seed (NOT random/time)."""
    if not candidates:
        return candidates
    n = len(candidates)
    start = seed % n
    return candidates[start:] + candidates[:start]


def do_inject(cfg: InjectConfig) -> int:
    existing = _read_manifest(cfg.repo_root)
    if existing and not cfg.force:
        _log(
            "REFUSING: an active chaos manifest already exists "
            f"({existing.get('target_file')}::{existing.get('function')}). "
            "Use --revert first, or --force to override."
        )
        return 3
    if existing and cfg.force:
        # --force must FIRST restore the prior mutation, else we would leave two
        # bugs on disk and the prior target's test stays red (breaking the
        # green-pre check for any candidate touching that file).
        _log("--force: reverting prior active mutation before re-injecting.")
        do_revert(cfg)

    # CHAOS READINESS HANDSHAKE -- never mutate into a dead bus. If a readiness
    # surface is configured (and the probe is enabled), wait for O+V's bus +
    # TestWatcher to be listening before touching a single byte. A graceful
    # timeout aborts the inject with a clear locus instead of mutating blind.
    check = _build_readiness_check(cfg)
    if check is not None and readiness_probe_enabled():
        _log("readiness handshake: probing O+V bus/TestWatcher before mutating...")
        ready, locus = asyncio.run(await_chaos_readiness(check))
        if not ready:
            _log(
                f"readiness ABORT [{locus}]: O+V not ready; no mutation performed."
            )
            print(json.dumps({"status": "aborted", "failure_locus": locus}, indent=2))
            return 7
        _log("readiness OK: O+V bus/TestWatcher live -- proceeding to mutate.")

    candidates = acquire_candidates(cfg)
    if not candidates:
        _log("no viable pure-leaf + test-bearing candidates found. Nothing to do.")
        return 4
    _log(f"acquired {len(candidates)} pure-leaf candidate(s) with plausible tests.")

    ordered = _seed_order(candidates, cfg.seed)
    attempts = 0
    for cand in ordered:
        if attempts >= cfg.max_attempts:
            break
        attempts += 1
        try:
            with open(cand.target_file, "r", encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        func = _select_function_node(src, cand.function, cand.lineno)
        if func is None:
            continue
        muts = _iter_mutations(src, func)
        if not muts:
            _log(f"skip {cand.function}: no viable mutation site.")
            continue

        if not cfg.verify_green:
            # No verification: trust the first mutation, mark red-post unknown.
            mut = muts[0]
            manifest = _build_manifest(cfg, cand, src, mut, cand.test_node)
            _write_manifest(cfg.repo_root, manifest)
            try:
                with open(cand.target_file, "w", encoding="utf-8") as fh:
                    fh.write(manifest["mutated_source"])
                _bump_mtime(cand.target_file)
            except OSError as exc:
                _log(f"file write failed for {cand.target_file}: {exc}; reverting manifest.")
                _delete_manifest(cfg.repo_root)
                continue
            manifest["test_red_post"] = None
            _write_manifest(cfg.repo_root, manifest)
            _log(
                f"INJECTED (unverified): {manifest['target_file']}::{cand.function} "
                f"line {mut.lineno} [{mut.kind}]. Manifest written."
            )
            _print_inject_summary(manifest)
            return 0

        # Find a GREEN test node (only a green test can be turned red). Try each
        # plausible node until one is confirmed green.
        green_node = None
        for tn in cand.test_nodes or [cand.test_node]:
            if _run_pytest_node(cfg.repo_root, tn, cfg.test_timeout_s) is True:
                green_node = tn
                break
        if green_node is None:
            _log(
                f"skip {cand.function}: no plausible test node confirmed green "
                "pre-injection."
            )
            continue

        # Try each mutation SITE until one turns the green node red.
        produced_red = False
        for mut in muts:
            try:
                mutated_src = _apply_mutation(src, mut)
            except ValueError as exc:
                _log(f"  site skip ({cand.function} L{mut.lineno}): {exc}")
                continue

            # --- MANIFEST BEFORE MUTATE (revert always possible) ---------- #
            manifest = _build_manifest(cfg, cand, src, mut, green_node)
            _write_manifest(cfg.repo_root, manifest)
            try:
                with open(cand.target_file, "w", encoding="utf-8") as fh:
                    fh.write(mutated_src)
                # Bump mtime so a .pyc cached during the green run is invalidated.
                _bump_mtime(cand.target_file)
            except OSError as exc:
                _log(f"file write failed for {cand.target_file}: {exc}; reverting.")
                _delete_manifest(cfg.repo_root)
                break

            post = _run_pytest_node(cfg.repo_root, green_node, cfg.test_timeout_s)
            if post is False:
                manifest["test_red_post"] = True
                _write_manifest(cfg.repo_root, manifest)
                _log(
                    f"INJECTED chaos: {manifest['target_file']}::{cand.function} "
                    f"line {mut.lineno} [{mut.kind}] -- test {green_node} "
                    "now RED (bug is genuinely detectable). Manifest written."
                )
                _print_inject_summary(manifest)
                return 0

            # Inert / indeterminate site -> REVERT this site and try the next.
            _log(
                f"  site INERT/indeterminate ({cand.function} L{mut.lineno} "
                f"[{mut.kind}], post={post}); reverting site."
            )
            try:
                with open(cand.target_file, "w", encoding="utf-8") as fh:
                    fh.write(src)
                _bump_mtime(cand.target_file)
            except OSError:
                _log("WARNING: revert write failed; manifest retains original_source.")
            _delete_manifest(cfg.repo_root)
        if produced_red:  # pragma: no cover - returned above on success
            return 0

    _log(
        f"exhausted candidates after {attempts} attempt(s) without producing a "
        "test-detectable bug. No mutation left on disk."
    )
    return 5


def _build_manifest(
    cfg: InjectConfig, cand: Candidate, src: str, mut: Mutation, test_node: str,
) -> dict:
    mutated_src = _apply_mutation(src, mut)
    return {
        "schema_version": 1,
        "injector_version": INJECTOR_VERSION,
        "target_file": os.path.relpath(cand.target_file, cfg.repo_root),
        "target_file_abs": cand.target_file,
        "function": cand.function,
        "line": mut.lineno,
        "original_source": src,
        "mutated_source": mutated_src,
        "mutation_kind": mut.kind,
        "mutation_detail": {
            "lineno": mut.lineno,
            "col_offset": mut.col_offset,
            "end_col_offset": mut.end_col_offset,
            "original_segment": mut.original_segment,
            "mutated_segment": mut.mutated_segment,
        },
        "test_node": test_node,
        "test_was_green_pre": bool(cfg.verify_green),
        "test_red_post": None,
        "injected_at_iso": cfg.now_iso,
        "seed": cfg.seed,
    }


def _print_inject_summary(manifest: dict) -> None:
    print(json.dumps({
        "status": "injected",
        "target_file": manifest["target_file"],
        "function": manifest["function"],
        "line": manifest["line"],
        "mutation_kind": manifest["mutation_kind"],
        "test_node": manifest["test_node"],
        "test_red_post": manifest["test_red_post"],
    }, indent=2))


def do_revert(cfg: InjectConfig) -> int:
    manifest = _read_manifest(cfg.repo_root)
    if not manifest:
        _log("no active chaos manifest; nothing to revert.")
        return 0
    abs_path = manifest.get("target_file_abs") or os.path.join(
        cfg.repo_root, manifest.get("target_file", ""),
    )
    original = manifest.get("original_source")
    if original is None:
        _log("manifest missing original_source; cannot revert safely. Aborting.")
        return 6
    try:
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(original)
        _bump_mtime(abs_path)
    except OSError as exc:
        _log(f"revert FAILED writing {abs_path}: {exc}")
        return 6
    _delete_manifest(cfg.repo_root)
    _log(
        f"REVERTED {manifest.get('target_file')}::{manifest.get('function')} "
        "to byte-identical original. Manifest cleared."
    )
    return 0


def do_status(cfg: InjectConfig) -> int:
    manifest = _read_manifest(cfg.repo_root)
    if not manifest:
        print(json.dumps({"active": False}, indent=2))
        _log("no active chaos manifest.")
        return 0
    print(json.dumps({
        "active": True,
        "target_file": manifest.get("target_file"),
        "function": manifest.get("function"),
        "line": manifest.get("line"),
        "mutation_kind": manifest.get("mutation_kind"),
        "test_node": manifest.get("test_node"),
        "test_was_green_pre": manifest.get("test_was_green_pre"),
        "test_red_post": manifest.get("test_red_post"),
        "injected_at_iso": manifest.get("injected_at_iso"),
        "injector_version": manifest.get("injector_version"),
    }, indent=2))
    return 0


def do_dry_run(cfg: InjectConfig) -> int:
    """Acquire + show the candidate + planned mutation. ZERO writes."""
    candidates = acquire_candidates(cfg)
    if not candidates:
        _log("dry-run: no viable candidates found.")
        return 4
    ordered = _seed_order(candidates, cfg.seed)
    for cand in ordered:
        try:
            with open(cand.target_file, "r", encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        func = _select_function_node(src, cand.function, cand.lineno)
        if func is None:
            continue
        mut = _plan_mutation(src, func)
        if mut is None:
            continue
        print(json.dumps({
            "status": "dry-run",
            "target_file": os.path.relpath(cand.target_file, cfg.repo_root),
            "function": cand.function,
            "line": mut.lineno,
            "planned_mutation_kind": mut.kind,
            "original_segment": mut.original_segment,
            "mutated_segment": mut.mutated_segment,
            "test_node": cand.test_node,
            "note": "NO writes performed (dry-run). Test not run.",
        }, indent=2))
        _log(
            f"dry-run candidate: {os.path.relpath(cand.target_file, cfg.repo_root)}"
            f"::{cand.function} line {mut.lineno} [{mut.kind}] (no writes)."
        )
        return 0
    _log("dry-run: candidates found but none had a viable mutation site.")
    return 5


def do_list_candidates(cfg: InjectConfig) -> int:
    """Show all viable pure-leaf + plausible-test targets (no test execution,
    no writes)."""
    candidates = acquire_candidates(cfg)
    if not candidates:
        _log("list-candidates: none found.")
        print(json.dumps({"count": 0, "candidates": []}, indent=2))
        return 4
    # Collapse duplicate (file, function) pairs across multiple test nodes.
    seen = set()
    rows = []
    for c in candidates:
        key = (c.target_file, c.function)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "target_file": os.path.relpath(c.target_file, cfg.repo_root),
            "function": c.function,
            "line": c.lineno,
            "test_node": c.test_node,
        })
    print(json.dumps({"count": len(rows), "candidates": rows}, indent=2))
    _log(f"list-candidates: {len(rows)} viable pure-leaf+test target(s).")
    return 0


# --------------------------------------------------------------------------- #
# Chaos Readiness Handshake -- never mutate into a dead bus.
# --------------------------------------------------------------------------- #
#
# THE BUG (A1 live soak): the injector mutated a file BEFORE O+V's
# TrinityEventBus + TestWatcher were initialized and listening, so the live
# ``fs.changed`` event fired into a void and the mutation was lost. Boot
# hydration recovers the offline case from ground truth; this handshake covers
# the live case by REFUSING to mutate until the bus + TestWatcher are
# demonstrably ready.
#
# The probe is ASYNC + bounded-deadline + exponential-backoff. It consults a
# readiness surface that O+V already exposes -- preferred: an HTTP health
# endpoint (``/channel/health`` / ``/observability/health``) carrying a
# ``testwatcher_ready`` field; alternative: the stdout/log BOOT-MARKER
# ``[TestWatcher] READY subscribed=fs.changed.*`` the TestFailureSensor emits
# once its fs.changed subscription is live. On timeout it returns a graceful
# (False, "CHAOS_READINESS_TIMEOUT") -- a clear locus, never a blind mutate.
#
# Gated ``JARVIS_CHAOS_READINESS_PROBE_ENABLED`` (default true). OFF -> the
# probe is a no-op that reports ready (legacy blind-inject), preserving the
# pre-handshake behavior byte-for-byte. NO ``time.sleep``: the synchronization
# is ``asyncio.sleep`` with env-tuned bounded deadlines.

TESTWATCHER_READY_MARKER = "[TestWatcher] READY subscribed=fs.changed.*"

ReadinessCheck = Callable[[], Awaitable[bool]]


def readiness_probe_enabled() -> bool:
    """Re-read ``JARVIS_CHAOS_READINESS_PROBE_ENABLED`` (default true)."""
    return os.environ.get(
        "JARVIS_CHAOS_READINESS_PROBE_ENABLED", "true",
    ).strip().lower() in ("true", "1", "yes")


def _readiness_deadline_s() -> float:
    """Bounded overall probe deadline (env ``JARVIS_CHAOS_READINESS_DEADLINE_S``)."""
    try:
        val = float(os.environ.get("JARVIS_CHAOS_READINESS_DEADLINE_S", "120"))
        return val if val > 0 else 120.0
    except (TypeError, ValueError):
        return 120.0


def _readiness_initial_backoff_s() -> float:
    """Initial backoff between polls (env ``JARVIS_CHAOS_READINESS_BACKOFF_S``)."""
    try:
        val = float(os.environ.get("JARVIS_CHAOS_READINESS_BACKOFF_S", "0.5"))
        return val if val > 0 else 0.5
    except (TypeError, ValueError):
        return 0.5


def _readiness_backoff_max_s() -> float:
    """Backoff ceiling (env ``JARVIS_CHAOS_READINESS_BACKOFF_MAX_S``)."""
    try:
        val = float(os.environ.get("JARVIS_CHAOS_READINESS_BACKOFF_MAX_S", "5"))
        return val if val > 0 else 5.0
    except (TypeError, ValueError):
        return 5.0


async def _fetch_health_json(url: str, timeout_s: float) -> Optional[dict]:
    """Fetch + parse a JSON health surface off-loop. Returns None on any error.

    Uses stdlib ``urllib`` in a worker thread so the probe stays non-blocking
    and the injector keeps its zero-backend-import discipline (no aiohttp dep).
    """
    def _blocking() -> Optional[dict]:
        import urllib.request

        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except Exception:  # noqa: BLE001 -- surface may not be up yet
            return None

    try:
        return await asyncio.to_thread(_blocking)
    except Exception:  # noqa: BLE001
        return None


def make_http_readiness_check(url: str, *, timeout_s: float = 2.0) -> ReadinessCheck:
    """Build an async readiness check that polls an HTTP health surface.

    Ready iff the JSON carries a truthy ``testwatcher_ready`` (or
    ``testwatcher_subscribed``) field. An unreachable / non-JSON surface is
    treated as not-ready (the probe keeps backing off), never an error.
    """
    async def _check() -> bool:
        data = await _fetch_health_json(url, timeout_s)
        if not isinstance(data, dict):
            return False
        return bool(
            data.get("testwatcher_ready") or data.get("testwatcher_subscribed")
        )

    return _check


def make_log_marker_readiness_check(
    log_path: str, *, marker: str = TESTWATCHER_READY_MARKER,
) -> ReadinessCheck:
    """Build an async readiness check that scans a log/stdout file for *marker*.

    Ready iff the boot-marker line is present. A missing / unreadable file is
    not-ready, never an error. The read is off-loop (worker thread).
    """
    async def _check() -> bool:
        def _blocking() -> bool:
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                    return marker in fh.read()
            except OSError:
                return False

        try:
            return await asyncio.to_thread(_blocking)
        except Exception:  # noqa: BLE001
            return False

    return _check


async def await_chaos_readiness(
    check: ReadinessCheck,
    *,
    deadline_s: Optional[float] = None,
    initial_backoff_s: Optional[float] = None,
    backoff_factor: float = 2.0,
    backoff_max_s: Optional[float] = None,
) -> Tuple[bool, str]:
    """Async, bounded, exponential-backoff wait for O+V to be inject-ready.

    Repeatedly awaits *check* (an async predicate over a readiness surface)
    until it returns True or the overall *deadline_s* elapses. Between polls it
    ``asyncio.sleep``s a backoff that grows by *backoff_factor* up to
    *backoff_max_s* -- NO fixed wait constant, no blocking synchronous sleep.
    A *check* that raises is swallowed (the surface may be transiently
    unreachable) and the probe keeps backing off.

    Returns
    -------
    ``(True, "")`` once ready, or ``(False, "CHAOS_READINESS_TIMEOUT")`` when
    the deadline elapses without readiness -- a graceful failure locus the
    caller surfaces instead of mutating into a dead bus.

    Gated ``JARVIS_CHAOS_READINESS_PROBE_ENABLED`` (default true). OFF returns
    ``(True, "")`` immediately WITHOUT consulting *check* (legacy blind inject).
    """
    if not readiness_probe_enabled():
        return (True, "")

    deadline = deadline_s if deadline_s is not None else _readiness_deadline_s()
    backoff = (
        initial_backoff_s
        if initial_backoff_s is not None
        else _readiness_initial_backoff_s()
    )
    backoff_ceiling = (
        backoff_max_s if backoff_max_s is not None else _readiness_backoff_max_s()
    )

    loop = asyncio.get_event_loop()
    start = loop.time()
    while True:
        try:
            if await check():
                return (True, "")
        except Exception as exc:  # noqa: BLE001 -- surface errors must not crash the probe
            _log("readiness check raised (treating as not-ready): %r" % (exc,))
        # Deadline check BEFORE sleeping so we don't oversleep past it.
        elapsed = loop.time() - start
        if elapsed >= deadline:
            _log(
                "CHAOS_READINESS_TIMEOUT after %.1fs -- O+V bus/TestWatcher not "
                "ready; refusing to mutate into a dead bus." % (elapsed,)
            )
            return (False, "CHAOS_READINESS_TIMEOUT")
        # Bound the sleep so we never overshoot the deadline.
        remaining = max(0.0, deadline - elapsed)
        await asyncio.sleep(min(backoff, backoff_ceiling, remaining) or backoff_ceiling)
        backoff = min(backoff * backoff_factor, backoff_ceiling)


def _build_readiness_check(cfg: "InjectConfig") -> Optional[ReadinessCheck]:
    """Pick the readiness surface from config / env. Returns None if none set.

    Preference: explicit HTTP URL -> explicit log-marker path. When neither is
    configured the handshake is skipped (the caller treats it as ready) so the
    injector stays usable standalone (dry-run, unit tests) without a live O+V.
    """
    url = (cfg.readiness_url or os.environ.get("JARVIS_CHAOS_READINESS_URL", "")).strip()
    if url:
        return make_http_readiness_check(url)
    log_path = (
        cfg.readiness_log or os.environ.get("JARVIS_CHAOS_READINESS_LOG", "")
    ).strip()
    if log_path:
        return make_log_marker_readiness_check(log_path)
    return None


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chaos_injector_ast.py",
        description="Dynamic AST Chaos Injector -- external saboteur for the A1 "
                    "Live-Fire Chaos Harness. Picks a pure-leaf function with a "
                    "green test and injects one minimal, test-detectable bug.",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inject", action="store_true",
                      help="Acquire a target, mutate, verify the test goes red, write manifest.")
    mode.add_argument("--revert", action="store_true",
                      help="Restore the original source from the active manifest.")
    mode.add_argument("--status", action="store_true",
                      help="Show the active chaos manifest (if any).")
    mode.add_argument("--dry-run", action="store_true",
                      help="Acquire + show candidate + planned mutation. ZERO writes.")
    mode.add_argument("--list-candidates", action="store_true",
                      help="List all viable pure-leaf + green-test targets found.")

    p.add_argument("--seed", type=int, default=0,
                   help="Deterministic candidate selection rotation (NOT random).")
    p.add_argument("--now", dest="now_iso", default="",
                   help="ISO timestamp stamped into the manifest (injected, not datetime.now).")
    p.add_argument("--force", action="store_true",
                   help="Override an existing active manifest on --inject.")
    p.add_argument("--repo-root", default=_REPO_ROOT,
                   help="Repo root (default: parent of scripts/).")
    p.add_argument("--test-timeout", type=float, default=60.0,
                   help="Per-test pytest timeout (seconds).")
    p.add_argument("--no-verify", action="store_true",
                   help="Skip pre/post pytest verification (NOT recommended).")
    p.add_argument("--readiness-url", default="",
                   help="HTTP health surface to probe for 'testwatcher_ready' "
                        "before mutating (Chaos Readiness Handshake). Env: "
                        "JARVIS_CHAOS_READINESS_URL.")
    p.add_argument("--readiness-log", default="",
                   help="Log/stdout file to scan for the TestWatcher READY "
                        "boot-marker before mutating. Env: "
                        "JARVIS_CHAOS_READINESS_LOG.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    cfg = InjectConfig(
        repo_root=os.path.abspath(args.repo_root),
        seed=int(args.seed),
        now_iso=args.now_iso,
        force=bool(args.force),
        test_timeout_s=float(args.test_timeout),
        verify_green=not bool(args.no_verify),
        readiness_url=str(args.readiness_url or ""),
        readiness_log=str(args.readiness_log or ""),
    )

    if args.inject:
        return do_inject(cfg)
    if args.revert:
        return do_revert(cfg)
    if args.status:
        return do_status(cfg)
    if args.dry_run:
        return do_dry_run(cfg)
    if args.list_candidates:
        return do_list_candidates(cfg)
    parser.error("no mode selected")  # pragma: no cover
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

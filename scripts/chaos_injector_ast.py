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
import hashlib
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
# Autonomous Self-Warming Oracle (Component 2) -- the injector serializes its
# already-computed isolation graph here so the Oracle (backend/) can warm its
# index for the known targets with ZERO JIT compute during the soak. STRICT
# separation: the injector WRITES this JSON; the Oracle READS it. No
# backend->scripts import. SHA256-validated on ingest (discard on mismatch).
PREWARM_REL_PATH = os.path.join(".jarvis", "oracle_prewarm.json")
PREWARM_SCHEMA_VERSION = 1


def _log(msg: str) -> None:
    """Loud structured logging."""
    print(f"[ChaosInjector] {msg}", file=sys.stderr)


# Process-global verbose flag for the evaluate-and-guarantee pipeline's
# hyper-observability traces (CONSTRAINT 4). Default ON for the injector's own
# logs; can be forced off via env. The async generator sets this from its cfg.
_VERBOSE: bool = True


def _verbose_enabled() -> bool:
    """Whether hyper-observability trace lines are emitted. Honors the
    process-global flag (set from InjectConfig.verbose) gated behind the env
    ``JARVIS_CHAOS_VERBOSE`` (default true) so an operator can silence it."""
    env = os.environ.get("JARVIS_CHAOS_VERBOSE", "")
    if env.strip():
        return env.strip().lower() in ("true", "1", "yes")
    return _VERBOSE


def _trace(channel: str, msg: str) -> None:
    """Structured hyper-observability trace line, e.g.
    ``[ChaosInjector][prevalidate] node=... reason=...``. Only emits when
    verbose is enabled."""
    if _verbose_enabled():
        print(f"[ChaosInjector][{channel}] {msg}", file=sys.stderr)


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
        # SEMANTIC PRE-VALIDATION (TASK 2): a function enters the pool ONLY if it
        # has a proven TYPE-SAFE mutation site. Zero-site functions (e.g. a
        # strictly-typed object-returner with no comparator/binop/bool/if/assign)
        # are disqualified HERE, at selection -- never dropped mid-inject. This
        # is the structural cure for the Omni-Soak ``inject:not_red``.
        if not has_viable_mutation(src, node):
            if _verbose_enabled():
                _trace("prevalidate", f"node={fi.name} file={abs_file} "
                                      "reason=no_mutation_site")
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


def _annotation_permits_return_none(func: ast.FunctionDef) -> bool:
    """Decide whether replacing a non-None return value with ``None`` is
    TYPE-SAFE (i.e. will NOT raise a fatal TypeError before the unit test's
    assertion can even run).

    Rule (CONSTRAINT 1, type-safe):
      * NO return annotation         -> permitted (we cannot prove a fatal type).
      * annotation is ``Optional[X]`` -> permitted (None is a legal value).
      * annotation is ``Any``         -> permitted.
      * annotation names ``None``     -> permitted (the function already returns None).
      * ANY OTHER annotation (a concrete required object type, e.g. ``-> Renderer``
        / ``-> StreamRenderer`` / ``-> dict``) -> BANNED, because returning None
        where a concrete non-Optional object is declared raises a fatal TypeError
        (Pydantic/runtime) before the assertion runs. We test the swarm's
        REASONING, not its crash-fixing.

    Conservative default: when the annotation cannot be confidently classified as
    Optional/Any/None, treat it as a concrete required type -> BANNED.
    """
    ann = func.returns
    if ann is None:
        return True

    def _ann_text(node: ast.AST) -> str:
        # Best-effort textual rendering for string-form and dotted annotations.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        try:
            return ast.unparse(node)  # py3.9+
        except Exception:  # pragma: no cover - extremely defensive
            return ""

    text = _ann_text(ann).strip()
    if not text:
        return False  # opaque -> conservative ban
    low = text.replace(" ", "")
    if low in ("None", "Any", "typing.Any"):
        return True
    # Optional[...] in any qualified form, or a "X | None" / "None | X" union.
    if low.startswith("Optional[") or low.startswith("typing.Optional["):
        return True
    if "|None" in low or "None|" in low:
        return True
    return False


def _iter_mutations(src: str, func: ast.FunctionDef) -> List[Mutation]:
    """Enumerate ALL viable minimal mutation sites for the function in a fixed,
    deterministic scan order: comparison-op flips, arithmetic-op flips,
    boolean-op flips, if-condition negation, assign-RHS literal/operator flips,
    return-literal alterations (string/bool/numeric), then -- LAST RESORT, only
    when TYPE-SAFE -- return-None. The caller tries them in order until one turns
    a green test red (some sites may be inert against a given assertion).

    CONSTRAINT 1 (type-safe): every emitted mutation yields a syntactically +
    type valid program that produces a WRONG-VALUE *logical* failure the unit test
    catches via assertion -- NOT a crash. return-None is offered ONLY when the
    return annotation does not declare a concrete required object type (see
    ``_annotation_permits_return_none``)."""
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

    # --- 4) If-condition negation (if cond: -> if not (cond):) ------------- #
    # Wrap the WHOLE test expression in ``not (...)`` so the branch flips. This is
    # a single-line, span-based edit over the test expression's source extent.
    for node in ast.walk(func):
        if isinstance(node, ast.If):
            _push(_negate_if_test(lines, node.test))

    # --- 5) Assign-RHS literal / operator flip ----------------------------- #
    # Mutate the right-hand side of an assignment (``x = <literal>`` or
    # ``x = a <op> b``) so a downstream return computed from it is wrong. Reuses
    # the constant-alteration + operator-flip primitives on the RHS.
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        rhs = node.value
        if isinstance(rhs, ast.Constant):
            m = _alter_constant(lines, rhs)
            if m:
                m.kind = "assign-literal:" + m.kind.split(":", 1)[-1]
                _push(m)
        elif isinstance(rhs, ast.BinOp):
            flip = _BINOP_FLIP.get(type(rhs.op))
            if flip:
                _from, _to, old_tok, new_tok = flip
                seg = _locate_op_token(
                    lines, rhs.left.end_lineno, rhs.left.end_col_offset,
                    rhs.right.lineno, rhs.right.col_offset, old_tok, new_tok,
                )
                if seg:
                    seg.kind = f"assign-binop:{_from}->{_to}"
                    _push(seg)

    # --- 6) Return-literal alteration (string / bool / numeric) ------------ #
    for node in ast.walk(func):
        if not (isinstance(node, ast.Return) and node.value is not None):
            continue
        if isinstance(node.value, ast.Constant):
            _push(_alter_constant(lines, node.value))

    # --- 7) Return-None (LAST RESORT, type-guarded) ------------------------ #
    # Only offered when (a) NO earlier vector produced any site AND (b) replacing
    # the value with None is TYPE-SAFE (no concrete required-object annotation).
    # This keeps a non-Optional object-returner from emitting a fatal-TypeError
    # vector while still covering the genuinely-untyped tail case.
    if not muts and _annotation_permits_return_none(func):
        for node in ast.walk(func):
            if not (isinstance(node, ast.Return) and node.value is not None):
                continue
            # Skip a return that is already a bare constant None or a simple
            # literal we would rather mutate in-place (handled above).
            if isinstance(node.value, ast.Constant) and node.value.value is None:
                continue
            _push(_return_none(lines, node.value))

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


def _negate_if_test(lines: List[str], test: ast.expr) -> Optional[Mutation]:
    """Wrap an ``if`` test expression in ``not (...)`` so the branch flips. The
    whole test span is replaced in place; single-line spans only (the test of an
    ``if`` is on its header line). Produces a WRONG-BRANCH logical failure the
    unit test catches -- always syntactically + type valid."""
    lineno = getattr(test, "lineno", None)
    end_lineno = getattr(test, "end_lineno", None)
    col = getattr(test, "col_offset", None)
    end_col = getattr(test, "end_col_offset", None)
    if None in (lineno, end_lineno, col, end_col):
        return None
    if lineno != end_lineno:
        return None  # multi-line test -> conservative skip
    if lineno < 1 or lineno > len(lines):
        return None
    line = lines[lineno - 1]
    if end_col > len(line):
        return None
    seg = line[col:end_col]
    new = f"not ({seg})"
    mut = _line_col_to_segment_edit(lines, lineno, col, end_col, seg, new)
    if mut:
        mut.kind = "if-negate"
    return mut


def _return_none(lines: List[str], val: ast.expr) -> Optional[Mutation]:
    """Replace a non-None return EXPRESSION with ``None`` (last-resort, only used
    when TYPE-SAFE per ``_annotation_permits_return_none``). Single-line span."""
    lineno = getattr(val, "lineno", None)
    end_lineno = getattr(val, "end_lineno", None)
    col = getattr(val, "col_offset", None)
    end_col = getattr(val, "end_col_offset", None)
    if None in (lineno, end_lineno, col, end_col):
        return None
    if lineno != end_lineno:
        return None
    if lineno < 1 or lineno > len(lines):
        return None
    line = lines[lineno - 1]
    if end_col > len(line):
        return None
    seg = line[col:end_col]
    if seg == "None":
        return None
    mut = _line_col_to_segment_edit(lines, lineno, col, end_col, seg, "None")
    if mut:
        mut.kind = "return-none"
    return mut


def has_viable_mutation(src: str, func: ast.FunctionDef) -> bool:
    """Semantic pre-validation (TASK 2): True iff the expanded ``_iter_mutations``
    yields >=1 TYPE-SAFE mutation site for this function. A function enters the
    candidate pool ONLY if this returns True -- a zero-site function is
    disqualified at SELECTION (not dropped mid-inject), killing ``inject:not_red``
    at its root. Reuses the single mutation engine; no parallel logic."""
    return bool(_iter_mutations(src, func))


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
# Autonomous Self-Warming Oracle -- pre-warm payload (Component 2).
# --------------------------------------------------------------------------- #

def _prewarm_path(repo_root: str) -> str:
    return os.path.join(repo_root, PREWARM_REL_PATH)


def _sha256_of_file(abs_path: str) -> Optional[str]:
    """SHA256 of a file's bytes (CONSTRAINT 3 -- cryptographic invalidation).
    ``None`` if unreadable (the target is dropped from the payload)."""
    try:
        with open(abs_path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _build_prewarm_payload(repo_root: str, target_abs_paths: Sequence[str]) -> dict:
    """Serialize the injector's already-computed isolation graph into a payload
    the Oracle can ingest to warm its index for the known targets.

    REUSES ``_files_are_import_coupled`` (the SAME import-graph math the
    Collision Matrix proves disjointness with) to record each target's coupled
    siblings -- no new dependency mapper. The SHA256 of each LIVE target file
    is recorded so a stale payload (file changed post-inject) is discarded on
    ingest -> JIT fallback. The hash is taken AT WRITE TIME against the
    mutated-on-disk file, which is exactly what the Oracle will re-hash."""
    uniq = []
    seen = set()
    for p in target_abs_paths:
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            uniq.append(ap)
    targets = []
    for ap in uniq:
        sha = _sha256_of_file(ap)
        if sha is None:
            continue  # unreadable -> drop (Oracle would discard anyway)
        coupled = [
            other for other in uniq
            if other != ap and _files_are_import_coupled(repo_root, ap, other)
        ]
        targets.append({
            "file_path": ap,
            "sha256": sha,
            "coupled": coupled,
        })
    return {
        "schema_version": PREWARM_SCHEMA_VERSION,
        "injector_version": INJECTOR_VERSION,
        "targets": targets,
    }


def _write_prewarm_payload(repo_root: str, target_abs_paths: Sequence[str]) -> None:
    """Atomically write the pre-warm payload. Fail-soft -- a write failure
    NEVER fails the inject (the Oracle simply falls back to the JIT)."""
    try:
        payload = _build_prewarm_payload(repo_root, target_abs_paths)
        path = _prewarm_path(repo_root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        _log(
            "pre-warm payload written: %d target(s) -> %s (Oracle warms its "
            "index without JIT compute during the soak)" % (
                len(payload["targets"]), PREWARM_REL_PATH,
            )
        )
    except Exception as exc:  # noqa: BLE001 -- never fail the inject
        _log("pre-warm payload write skipped (non-fatal): %s" % (exc,))


def _delete_prewarm_payload(repo_root: str) -> None:
    try:
        os.remove(_prewarm_path(repo_root))
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
    # CONSTRAINT 4: hyper-observability. Default ON for the injector's own logs.
    verbose: bool = True


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

    # schema_version 2 (decomposable): restore EVERY target in the list,
    # byte-identically. The manifest is cleared only if ALL restores succeed, so
    # a partial revert failure stays auditable (manifest retained).
    if manifest.get("schema_version") == DECOMPOSABLE_SCHEMA_VERSION:
        entries = manifest.get("targets") or []
        if not entries:
            _log("decomposable manifest has no targets; clearing.")
            _delete_manifest(cfg.repo_root)
            return 0
        all_ok = True
        for entry in entries:
            abs_path = entry.get("target_file_abs") or os.path.join(
                cfg.repo_root, entry.get("target_file", ""),
            )
            original = entry.get("original_source")
            if original is None:
                _log("decomposable revert: entry missing original_source for "
                     "%s; skipping (cannot restore safely)." % (abs_path,))
                all_ok = False
                continue
            if not _restore_target(abs_path, original):
                all_ok = False
        if not all_ok:
            _log("decomposable revert: one or more targets FAILED to restore; "
                 "retaining manifest for audit.")
            return 6
        _delete_manifest(cfg.repo_root)
        # Self-Warming Oracle: the targets are restored, so the pre-warm
        # payload's hashes are now stale -> remove it (the Oracle would
        # discard a mismatch anyway, but a clean revert leaves no artifact).
        _delete_prewarm_payload(cfg.repo_root)
        _log("REVERTED %d decomposable target(s) to byte-identical originals. "
             "Manifest cleared." % (len(entries),))
        return 0

    # schema_version 1 (legacy single-target) -- unchanged behavior.
    abs_path = manifest.get("target_file_abs") or os.path.join(
        cfg.repo_root, manifest.get("target_file", ""),
    )
    original = manifest.get("original_source")
    if original is None:
        _log("manifest missing original_source; cannot revert safely. Aborting.")
        return 6
    if not _restore_target(abs_path, original):
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
# DecomposableChaosInjector -- N MUTUALLY-ISOLATED pure-leaf targets.
# --------------------------------------------------------------------------- #
#
# THE POINT: O+V's L3 AST Collision Matrix fans a set of work units out into
# parallel subagents ONLY when their target files are pairwise import-disjoint
# (no two files import each other / share an interface<->impl coupling). A
# single-target chaos op can never exercise that fan-out. This mode acquires N
# pure-leaf functions in N DIFFERENT files, PROVES pairwise import isolation via
# a bounded AST import scan (the same shape oracle.py uses), mutates each so its
# OWN test goes red, and records ALL N in one manifest (schema_version 2).
#
# Zero-trust: if N mutually-isolated, green-tested leaves cannot be found we
# return FEWER and log honestly -- never fabricate a target. Any one target that
# fails to go red is reverted + dropped (shrink); the inject NEVER leaves a
# half-mutated tree (partial-failure cleanup reverts every already-applied
# target). REVERT iterates the manifest list and restores ALL N byte-identically.
#
# Reuse-first: purity (_PurityVisitor / _analyze_function), candidate scan
# (acquire_candidates), mutation planning (_iter_mutations), green/red
# verification (_run_pytest_node), manifest IO (_write/_read/_delete_manifest)
# and the byte-identical revert primitive are ALL reused unchanged. This mode
# only adds the import-isolation predicate + the N-target orchestration.

DECOMPOSABLE_SCHEMA_VERSION = 2


@dataclass
class ChaosTarget:
    """One acquired, mutation-ready isolated target within a decomposable set."""
    target_file: str               # absolute path (alias of candidate.target_file)
    function: str
    lineno: int
    end_lineno: int
    test_node: str                 # the green test node confirmed pre-injection
    test_nodes: List[str] = field(default_factory=list)
    dotted_name: str = ""          # module dotted name (for the import graph)
    depth: int = 0                 # 0 = pure leaf, 1 = one-level-deep node
    # The EXACT mutation proven (during pre-validation) to turn ``test_node`` red,
    # non-destructively. The inject path RE-APPLIES this proven site, so a yielded
    # target is GUARANTEED reddenable -- no select-then-drop, no inject:not_red.
    proven_mutation: Optional[Mutation] = field(default=None)


def _module_dotted_name(repo_root: str, abs_file: str) -> str:
    """Map an absolute .py path to its dotted module name relative to the repo
    root (e.g. <repo>/backend/utils/calc0.py -> backend.utils.calc0). Mirrors the
    package-path math oracle.py uses for its import graph. ``__init__.py`` maps
    to the package dotted name. Returns "" for paths outside the repo."""
    try:
        rel = os.path.relpath(os.path.abspath(abs_file), os.path.abspath(repo_root))
    except ValueError:
        return ""
    if rel.startswith(".."):
        return ""
    rel = rel[:-3] if rel.endswith(".py") else rel
    parts = [p for p in rel.split(os.sep) if p and p != "."]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _file_import_targets(abs_file: str) -> set:
    """Return the set of dotted import targets a file references, via a bounded
    AST scan (NO import execution). Covers both ``import a.b.c`` and
    ``from a.b import name`` (the module is ``a.b``; relative imports keep their
    leading-dot prefix so a sibling resolve can match). Best-effort: an
    unparseable / unreadable file yields an empty set (treated as importing
    nothing -- conservative for the *coupling* test, which only adds edges)."""
    out: set = set()
    try:
        with open(abs_file, "r", encoding="utf-8") as fh:
            src = fh.read()
    except (OSError, UnicodeDecodeError):
        return out
    try:
        tree = ast.parse(src, filename=abs_file)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level and node.level > 0:
                # Relative import: preserve dots so the caller can resolve it
                # against the importer's own package.
                out.add("." * node.level + mod)
            elif mod:
                out.add(mod)
                # ``from a.b import c`` may name a submodule a.b.c -- record both
                # the package and each fully-qualified candidate so a direct
                # sibling-module import is detected.
                for alias in node.names:
                    if alias.name:
                        out.add(mod + "." + alias.name)
    return out


def _resolve_relative(importer_dotted: str, rel_target: str) -> str:
    """Resolve a relative import token (leading dots + module) against the
    importer's dotted module name. ``importer=backend.utils.coupled_b`` +
    ``rel=.coupled_a`` -> ``backend.utils.coupled_a``."""
    level = len(rel_target) - len(rel_target.lstrip("."))
    suffix = rel_target.lstrip(".")
    base_parts = importer_dotted.split(".") if importer_dotted else []
    # ``from . import x`` (level=1) is relative to the importer's package, so we
    # drop the module's own name (one component) per extra level beyond the pkg.
    drop = level
    anchor = base_parts[: max(0, len(base_parts) - drop)]
    if suffix:
        anchor = anchor + suffix.split(".")
    return ".".join(anchor)


def _files_are_import_coupled(repo_root: str, file_a: str, file_b: str) -> bool:
    """SYMMETRIC predicate: do these two files import-couple (does either import
    the other's module)? Bounded AST scan against the real dotted-name graph --
    no import execution, no hardcoding. The Collision Matrix marks a pair
    DISJOINT iff this returns False.

    Coupling exists iff A imports B's module (or a submodule under it), or vice
    versa. Resolves both absolute (``backend.utils.x``) and relative
    (``.x`` / ``..pkg.x``) import forms."""
    a_abs, b_abs = os.path.abspath(file_a), os.path.abspath(file_b)
    if a_abs == b_abs:
        return True  # same file -> can never be two parallel units
    a_dotted = _module_dotted_name(repo_root, a_abs)
    b_dotted = _module_dotted_name(repo_root, b_abs)

    def _imports_other(importer_abs: str, importer_dotted: str, other_dotted: str) -> bool:
        if not other_dotted:
            return False
        for tok in _file_import_targets(importer_abs):
            resolved = (
                _resolve_relative(importer_dotted, tok)
                if tok.startswith(".")
                else tok
            )
            if not resolved:
                continue
            # Exact module hit, or other_dotted is a package prefix of the import
            # (importer pulls a submodule of other), or other pulls a submodule
            # under the import token.
            if (
                resolved == other_dotted
                or resolved.startswith(other_dotted + ".")
                or other_dotted.startswith(resolved + ".")
            ):
                return True
        return False

    return (
        _imports_other(a_abs, a_dotted, b_dotted)
        or _imports_other(b_abs, b_dotted, a_dotted)
    )


def _candidate_to_target(repo_root: str, cand: Candidate) -> ChaosTarget:
    return ChaosTarget(
        target_file=cand.target_file,
        function=cand.function,
        lineno=cand.lineno,
        end_lineno=cand.end_lineno,
        test_node=cand.test_node,
        test_nodes=list(cand.test_nodes or [cand.test_node]),
        dotted_name=_module_dotted_name(repo_root, cand.target_file),
    )


def _confirm_green(cfg: InjectConfig, cand: Candidate) -> Optional[str]:
    """Return the FIRST plausible test node confirmed GREEN pre-injection, or
    None. Reuses _run_pytest_node. When verify_green is off, trusts the primary
    node (mirrors do_inject's --no-verify path)."""
    if not cfg.verify_green:
        return cand.test_node
    for tn in (cand.test_nodes or [cand.test_node]):
        if _run_pytest_node(cfg.repo_root, tn, cfg.test_timeout_s) is True:
            return tn
    return None


def _prove_reddenable(
    cfg: InjectConfig, abs_file: str, func_name: str, lineno: int, test_node: str,
) -> Optional[Mutation]:
    """NON-DESTRUCTIVE red-proof: find a mutation site that ACTUALLY turns
    ``test_node`` red, restoring the file byte-identically afterward. Returns the
    proven Mutation (so the inject path re-applies the exact site) or None if NO
    site reddens this test.

    This is the structural cure for ``inject:not_red``: ``has_viable_mutation``
    proves a TYPE-SAFE site EXISTS, but a site can be semantically INERT for a
    given assertion (e.g. ``return None`` when the global is already None). By
    proving red-ness BEFORE a target enters the pool, a yielded target is
    GUARANTEED reddenable -- no select-then-drop. Reuses _iter_mutations +
    _apply_mutation + _run_pytest_node; the file is always restored (revert-ALWAYS
    via try/finally)."""
    try:
        with open(abs_file, "r", encoding="utf-8") as fh:
            src = fh.read()
    except (OSError, UnicodeDecodeError):
        return None
    func = _select_function_node(src, func_name, lineno)
    if func is None:
        return None
    muts = _iter_mutations(src, func)
    if not muts:
        return None
    # When verification is off we cannot prove red-ness; trust the first site.
    if not cfg.verify_green:
        return muts[0]

    proven: Optional[Mutation] = None
    try:
        for mut in muts:
            try:
                mutated_src = _apply_mutation(src, mut)
            except ValueError:
                continue
            try:
                with open(abs_file, "w", encoding="utf-8") as fh:
                    fh.write(mutated_src)
                _bump_mtime(abs_file)
            except OSError:
                break
            post = _run_pytest_node(cfg.repo_root, test_node, cfg.test_timeout_s)
            # Restore immediately (non-destructive) before judging.
            try:
                with open(abs_file, "w", encoding="utf-8") as fh:
                    fh.write(src)
                _bump_mtime(abs_file)
            except OSError:
                _log("WARNING: red-proof restore failed for %s" % (abs_file,))
            if post is False:
                proven = mut
                break
    finally:
        # Revert-ALWAYS: guarantee the original source is on disk no matter what.
        try:
            with open(abs_file, "w", encoding="utf-8") as fh:
                fh.write(src)
            _bump_mtime(abs_file)
        except OSError:
            _log("WARNING: red-proof final restore failed for %s" % (abs_file,))
    return proven


# --------------------------------------------------------------------------- #
# Adaptive depth analysis (depth-0 pure leaves -> depth-1 one-level-deep nodes).
# --------------------------------------------------------------------------- #
#
# Reuse-first: depth-0 candidates ARE the existing acquire_candidates() output
# (purity rejects any function calling a non-allowlisted name, so a depth-0
# candidate is a true leaf). Depth-1 candidates RELAX that single rule: a
# function may call helper functions PROVIDED every such helper is itself a
# depth-0 pure leaf in the SAME repo. CONSTRAINT 2: two depth-1 candidates are
# co-selectable only if their depth-0 dependency SETS are disjoint.


def _depth1_relaxed_purity(node: ast.FunctionDef, leaf_names: set) -> bool:
    """Is ``node`` a DEPTH-1 candidate? Reuses _PurityVisitor with a single
    relaxation: a bare-name call to a name in ``leaf_names`` (a proven depth-0
    pure leaf) is allowed. Everything else _PurityVisitor rejects still rejects.
    Conservative: only ONE level deep -- the helper must be a known pure leaf."""
    arg_names = [a.arg for a in node.args.args]
    visitor = _PurityVisitor(arg_names)
    # Relax: pre-seed the allowlist with the known depth-0 leaf names so a bare
    # call to one of them does not flip purity. We do this by monkeypatching the
    # visit_Call to consult leaf_names first (no fork of the visitor class).
    base_visit_call = visitor.visit_Call

    def _relaxed_call(call: ast.Call) -> None:
        func = call.func
        if isinstance(func, ast.Name) and func.id in leaf_names:
            # Allowed depth-0 helper call; still recurse into its arguments.
            for a in call.args:
                visitor.visit(a)
            for kw in call.keywords:
                visitor.visit(kw.value)
            return
        base_visit_call(call)

    visitor.visit_Call = _relaxed_call  # type: ignore[assignment]
    if not node.body:
        return False
    has_return_value = any(
        isinstance(n, ast.Return) and n.value is not None for n in ast.walk(node)
    )
    if not has_return_value:
        return False
    for stmt in node.body:
        visitor.visit(stmt)
        if not visitor.pure:
            return False
    return True


def _depth0_dependency_set(repo_root: str, abs_file: str, func_name: str) -> set:
    """The set of depth-0 dependency identities a function depends on: the dotted
    names of the pure-leaf helpers it calls (resolved against the file's imports +
    same-module defs). For a pure depth-0 leaf this is EMPTY. For a depth-1 node
    it carries the leaves it fans into. Two depth-1 targets are co-selectable only
    if these sets are disjoint (CONSTRAINT 2). Bounded AST scan; no execution."""
    out: set = set()
    try:
        with open(abs_file, "r", encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename=abs_file)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return out
    func = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            func = node
            break
    if func is None:
        return out
    # Map locally-imported names -> dotted source. ``from a.b import c`` => c -> a.b.c
    name_to_dotted: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                local = alias.asname or alias.name
                name_to_dotted[local] = node.module + "." + alias.name
    same_mod = _module_dotted_name(repo_root, abs_file)
    # Same-module helper defs resolve to <module>.<name>.
    local_defs = {
        n.name for n in tree.body if isinstance(n, ast.FunctionDef)
    }
    for call in ast.walk(func):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
            nm = call.func.id
            if nm in _PURE_BUILTIN_CALLS:
                continue
            if nm in name_to_dotted:
                out.add(name_to_dotted[nm])
            elif nm in local_defs and nm != func_name:
                out.add(f"{same_mod}.{nm}" if same_mod else nm)
    return out


def _depth1_scan_dirs(repo_root: str) -> List[str]:
    """The broadened file surface for the ADAPTIVE depth-1 gear-shift. When the
    narrow pure-util SAFE-token surface (depth-0) is exhausted, depth-1 reaches
    one level deeper into the tree -- so it scans all of ``backend/`` (still
    DENYLIST-ABSOLUTE, still purity-gated, still reddenable-gated). Env
    ``JARVIS_CHAOS_TARGET_DIRS`` overrides the surface entirely (operator choice).
    """
    env = (os.environ.get("JARVIS_CHAOS_TARGET_DIRS", "") or "").strip()
    if env:
        return _default_target_dirs(repo_root)
    backend = os.path.join(repo_root, "backend")
    return [backend] if os.path.isdir(backend) else _default_target_dirs(repo_root)


def _scan_depth1_candidates(cfg: InjectConfig) -> List[Candidate]:
    """Scan for DEPTH-1 candidates: functions one level deep (calling only proven
    depth-0 pure leaves) that ALSO have a viable type-safe mutation site and a
    plausible test. Reuses the file walk + test discovery + has_viable_mutation;
    only the purity bound is relaxed via _depth1_relaxed_purity. The file surface
    is BROADENED (``_depth1_scan_dirs``) since depth-1 is the adaptive widening."""
    repo_root = cfg.repo_root
    target_dirs = _depth1_scan_dirs(repo_root)
    test_files = _find_test_files(repo_root)

    # 1) Collect the set of all depth-0 pure-leaf function NAMES across the surface
    #    (the allowlist of helpers a depth-1 node may call).
    leaf_names: set = set()
    files: List[str] = []
    seen = set()

    def _add_file(abs_file: str) -> None:
        if abs_file in seen or _is_denied(abs_file):
            return
        seen.add(abs_file)
        files.append(abs_file)

    for tdir in target_dirs:
        for dirpath, dirnames, fns in os.walk(tdir):
            dirnames[:] = [
                d for d in dirnames
                if d not in ("__pycache__", "tests", "test", "migrations", "node_modules")
            ]
            for f in sorted(fns):
                if f.endswith(".py"):
                    _add_file(os.path.join(dirpath, f))

    parsed: dict = {}
    for abs_file in files:
        try:
            with open(abs_file, "r", encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src, filename=abs_file)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        parsed[abs_file] = (src, tree)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and _analyze_function(node).pure:
                leaf_names.add(node.name)

    # The NARROW (SAFE-token) surface already covered by acquire_candidates -- so
    # the gear-shift does not re-offer those exact (file, function) pairs.
    narrow_seen = {
        (c.target_file, c.function) for c in acquire_candidates(cfg)
    }

    # 2) Scan the BROADENED surface for two flavours of newly-reachable target:
    #    (a) genuine depth-0 pure leaves that the narrow SAFE-token surface missed
    #        (empty dep-set -> trivially disjoint), and
    #    (b) true depth-1 nodes (one level deep, calling only proven leaves).
    out: List[Candidate] = []
    for abs_file, (src, tree) in parsed.items():
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            is_leaf = _analyze_function(node).pure
            if is_leaf:
                # A broadened depth-0 leaf is a legitimate gear-shift target ONLY
                # if the narrow pass did not already surface it.
                if (abs_file, node.name) in narrow_seen:
                    continue
            elif not _depth1_relaxed_purity(node, leaf_names):
                continue
            if not has_viable_mutation(src, node):
                continue
            nodes = _test_nodes_for_function(repo_root, abs_file, node.name, test_files)
            if not nodes:
                continue
            nodes = sorted(set(nodes))
            out.append(Candidate(
                target_file=abs_file,
                function=node.name,
                lineno=node.lineno,
                end_lineno=getattr(node, "end_lineno", node.lineno),
                test_node=nodes[0],
                test_nodes=nodes,
            ))
    out.sort(key=lambda c: (c.target_file, c.function, c.test_node))
    return out


async def generate_verified_targets(cfg: InjectConfig, n: int):
    """ASYNC dynamic pool generator (TASK 3): yield exactly ``n`` verified targets
    one at a time, stopping at N. Each yield passes, IN ORDER:
      1) pure-leaf (depth-0) -- from acquire_candidates;
      2) has_viable_mutation (TASK 2) -- a proven type-safe site;
      3) mutually-isolated from ALL prior yields (import-disjoint files);
      4) green test (_run_pytest_node off-loop via run_in_executor -- non-blocking).

    ADAPTIVE SCOPE: when depth-0 pure-leaves are EXHAUSTED before N, shift to
    DEPTH-1 nodes (functions one level deep), reusing the purity analysis with a
    relaxed depth bound. CONSTRAINT 2: two selected depth-1 functions must have
    pairwise-DISJOINT depth-0 dependency sub-graphs (else the swarm agents collide
    fixing a shared dep).

    Hyper-observability (CONSTRAINT 4): a ``[prevalidate]`` line per rejected node
    with the exact reason, a loud ``[adaptive]`` line on the depth gear-shift, and
    a ``[yield]`` line per accepted target."""
    global _VERBOSE
    _VERBOSE = bool(cfg.verbose)
    loop = asyncio.get_event_loop()
    yielded = 0
    chosen_files: List[str] = []
    chosen_dep_sets: List[set] = []

    async def _is_green(cand: Candidate) -> Optional[str]:
        if not cfg.verify_green:
            return cand.test_node
        for tn in (cand.test_nodes or [cand.test_node]):
            res = await loop.run_in_executor(
                None, _run_pytest_node, cfg.repo_root, tn, cfg.test_timeout_s,
            )
            if res is True:
                return tn
        return None

    def _isolated(cand: Candidate) -> bool:
        return not any(
            _files_are_import_coupled(cfg.repo_root, cand.target_file, picked)
            for picked in chosen_files
        )

    # ---- Depth-0 pass ----------------------------------------------------- #
    depth0 = acquire_candidates(cfg)
    by_file: List[Candidate] = []
    seen_files = set()
    for cand in _seed_order(depth0, cfg.seed):
        if cand.target_file in seen_files:
            continue
        seen_files.add(cand.target_file)
        by_file.append(cand)

    for cand in by_file:
        if yielded >= n:
            return
        # (2) has_viable_mutation is already guaranteed by acquire_candidates'
        # pre-validation gate, but re-affirm against the live source.
        if not _isolated(cand):
            _trace("prevalidate", f"node={cand.function} file={cand.target_file} "
                                  "reason=coupled:prior")
            continue
        green = await _is_green(cand)
        if green is None:
            _trace("prevalidate", f"node={cand.function} file={cand.target_file} "
                                  "reason=test_not_green")
            continue
        cand.test_node = green
        # GUARANTEE reddenable (non-destructive proof) BEFORE entering the pool --
        # this is what kills the Omni-Soak ``inject:not_red`` (a type-safe site can
        # still be semantically inert for THIS assertion).
        proven = await loop.run_in_executor(
            None, _prove_reddenable, cfg, cand.target_file, cand.function,
            cand.lineno, green,
        )
        if proven is None:
            _trace("prevalidate", f"node={cand.function} file={cand.target_file} "
                                  "reason=not_reddenable")
            continue
        tgt = _candidate_to_target(cfg.repo_root, cand)
        tgt.depth = 0
        tgt.proven_mutation = proven
        chosen_files.append(cand.target_file)
        chosen_dep_sets.append(set())  # depth-0 leaf has no depth-0 deps
        yielded += 1
        _trace("yield", f"target={cand.function} file={cand.target_file} "
                        f"mutation={proven.kind} depth=0")
        yield tgt

    if yielded >= n:
        return

    # ---- Adaptive depth-1 expansion --------------------------------------- #
    _log(
        f"[ChaosInjector][adaptive] depth-0 leaves exhausted (found {yielded}/{n}) "
        "-> expanding to depth-1"
    )
    depth1 = _scan_depth1_candidates(cfg)
    by_file_d1: List[Candidate] = []
    seen_d1 = set(chosen_files)
    for cand in _seed_order(depth1, cfg.seed):
        if cand.target_file in seen_d1:
            continue
        seen_d1.add(cand.target_file)
        by_file_d1.append(cand)

    for cand in by_file_d1:
        if yielded >= n:
            return
        if not _isolated(cand):
            _trace("prevalidate", f"node={cand.function} file={cand.target_file} "
                                  "reason=coupled:prior")
            continue
        dep_set = _depth0_dependency_set(cfg.repo_root, cand.target_file, cand.function)
        # CONSTRAINT 2: pairwise-disjoint depth-0 dependency sub-graphs.
        shared = None
        for prior in chosen_dep_sets:
            inter = dep_set & prior
            if inter:
                shared = sorted(inter)[0]
                break
        if shared is not None:
            _trace("prevalidate", f"node={cand.function} file={cand.target_file} "
                                  f"reason=shared_depth0_dep:{shared}")
            continue
        green = await _is_green(cand)
        if green is None:
            _trace("prevalidate", f"node={cand.function} file={cand.target_file} "
                                  "reason=test_not_green")
            continue
        cand.test_node = green
        proven = await loop.run_in_executor(
            None, _prove_reddenable, cfg, cand.target_file, cand.function,
            cand.lineno, green,
        )
        if proven is None:
            _trace("prevalidate", f"node={cand.function} file={cand.target_file} "
                                  "reason=not_reddenable")
            continue
        tgt = _candidate_to_target(cfg.repo_root, cand)
        tgt.depth = 1
        tgt.proven_mutation = proven
        chosen_files.append(cand.target_file)
        chosen_dep_sets.append(dep_set)
        yielded += 1
        _trace("yield", f"target={cand.function} file={cand.target_file} "
                        f"mutation={proven.kind} depth=1")
        yield tgt


def acquire_isolated_targets(cfg: InjectConfig, n: int = 3) -> List[ChaosTarget]:
    """Acquire up to ``n`` pure-leaf targets, each in a DIFFERENT file, such that
    every pair is import-DISJOINT (verified via the real AST import graph) AND
    each independently has a GREEN test.

    Now backed by the async ``generate_verified_targets`` pipeline (TASK 4): it
    requests N and is GUARANTEED N pre-validated, mutatable, mutually-isolated,
    green-tested targets (depth-0 leaves first, then disjoint-subgraph depth-1
    nodes). Zero-trust: if fewer than ``n`` exist even after depth-1 expansion,
    returns FEWER -- never fabricates. Reuses acquire_candidates + the green-test
    confirmation. Kept synchronous for the existing call sites by driving the
    async generator to completion."""
    if n <= 0:
        return []

    async def _collect() -> List[ChaosTarget]:
        out: List[ChaosTarget] = []
        gen = generate_verified_targets(cfg, n)
        try:
            async for tgt in gen:
                out.append(tgt)
        finally:
            # CONSTRAINT 3: the generator's failure/interrupt path cleans up. The
            # generator itself only READS (it never mutates), so closing it here
            # leaves no disk state; aclose() runs its finally blocks.
            await gen.aclose()
        return out

    chosen = asyncio.run(_collect())
    if len(chosen) < n:
        _log(
            "acquire_isolated_targets: requested %d but only %d mutually-isolated "
            "green-tested pure-leaf target(s) available even after depth-1 "
            "expansion (honest: fewer, NOT fabricated)." % (n, len(chosen))
        )
    return chosen


def _mutate_one_target_red(
    cfg: InjectConfig, tgt: ChaosTarget,
) -> Optional[Tuple[dict, str]]:
    """Apply the existing single-mutation logic to ONE target and confirm its OWN
    test goes red. On success returns ``(entry, original_src)`` where ``entry`` is
    the per-target manifest dict; the file is left MUTATED on disk. On failure
    (no viable site / nothing turns red) the file is restored byte-identically
    and None is returned. Reuses _iter_mutations + _apply_mutation +
    _run_pytest_node entirely."""
    try:
        with open(tgt.target_file, "r", encoding="utf-8") as fh:
            src = fh.read()
    except (OSError, UnicodeDecodeError):
        return None
    func = _select_function_node(src, tgt.function, tgt.lineno)
    if func is None:
        return None
    muts = _iter_mutations(src, func)
    if not muts:
        _log("decomposable: %s has no viable mutation site." % (tgt.function,))
        return None

    # Prefer the mutation the generator already PROVED reddens this target's test
    # (non-destructive pre-validation) -- so the inject is guaranteed, not a
    # re-search. Fall back to the full ordered scan only if no proof was carried.
    if tgt.proven_mutation is not None:
        muts = [tgt.proven_mutation] + [
            m for m in muts
            if (m.lineno, m.col_offset, m.end_col_offset, m.mutated_segment)
            != (tgt.proven_mutation.lineno, tgt.proven_mutation.col_offset,
                tgt.proven_mutation.end_col_offset, tgt.proven_mutation.mutated_segment)
        ]

    for mut in muts:
        try:
            mutated_src = _apply_mutation(src, mut)
        except ValueError as exc:
            _log("  decomposable site skip (%s L%d): %s" % (tgt.function, mut.lineno, exc))
            continue
        try:
            with open(tgt.target_file, "w", encoding="utf-8") as fh:
                fh.write(mutated_src)
            _bump_mtime(tgt.target_file)
        except OSError as exc:
            _log("decomposable write failed for %s: %s" % (tgt.target_file, exc))
            return None

        if cfg.verify_green:
            post = _run_pytest_node(cfg.repo_root, tgt.test_node, cfg.test_timeout_s)
            went_red = post is False
        else:
            went_red = True  # trust (no verification requested)

        if went_red:
            entry = {
                "target_file": os.path.relpath(tgt.target_file, cfg.repo_root),
                "target_file_abs": tgt.target_file,
                "function": tgt.function,
                "line": mut.lineno,
                "dotted_name": tgt.dotted_name,
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
                "test_node": tgt.test_node,
                "test_was_green_pre": bool(cfg.verify_green),
                "test_red_post": bool(cfg.verify_green) or None,
            }
            return entry, src

        # Inert site -> restore + try next site.
        try:
            with open(tgt.target_file, "w", encoding="utf-8") as fh:
                fh.write(src)
            _bump_mtime(tgt.target_file)
        except OSError:
            _log("WARNING: decomposable site-revert write failed for %s" % (tgt.target_file,))

    return None


def _restore_target(target_file_abs: str, original_source: str) -> bool:
    """Byte-identical restore of one target (reused by partial-failure cleanup
    and do_revert). Returns True on success."""
    try:
        with open(target_file_abs, "w", encoding="utf-8") as fh:
            fh.write(original_source)
        _bump_mtime(target_file_abs)
        return True
    except OSError as exc:
        _log("restore FAILED for %s: %s" % (target_file_abs, exc))
        return False


def do_inject_decomposable(
    cfg: InjectConfig, n: int = 3, *, require_exact: bool = False,
) -> int:
    """Acquire + mutate up to ``n`` MUTUALLY-ISOLATED pure-leaf targets so each
    turns its OWN test red, then write ONE schema_version-2 manifest carrying the
    full target list.

    ``require_exact=True`` -> refuse (no mutation) unless exactly ``n`` isolated
    green targets both exist AND go red. Default (False) -> inject as many as can
    be turned red and report honestly (zero-trust: fewer over fabricate).

    PARTIAL-FAILURE CLEANUP: any target that cannot be turned red is dropped and
    every already-mutated target is restored if the final set is unacceptable, so
    the tree is NEVER left half-injected.

    Returns 0 on success (>=1 target red, or exactly n when require_exact), non-0
    otherwise (with the tree restored byte-identically)."""
    global _VERBOSE
    _VERBOSE = bool(cfg.verbose)
    existing = _read_manifest(cfg.repo_root)
    if existing and not cfg.force:
        _log(
            "REFUSING: an active chaos manifest already exists. Use --revert "
            "first, or --force to override."
        )
        return 3
    if existing and cfg.force:
        _log("--force: reverting prior active mutation before decomposable inject.")
        do_revert(cfg)

    targets = acquire_isolated_targets(cfg, n=n)
    if require_exact and len(targets) < n:
        _log(
            "require_exact: only %d/%d isolated targets acquired -- refusing to "
            "inject a partial set. No mutation performed." % (len(targets), n)
        )
        return 4
    if not targets:
        _log("decomposable: no isolated pure-leaf targets acquired. Nothing to do.")
        return 4

    applied: List[dict] = []  # successfully-reddened per-target manifest entries
    accepted = False  # set True only once the manifest is durably written

    def _rollback_all() -> None:
        for entry in applied:
            _restore_target(entry["target_file_abs"], entry["original_source"])
        applied.clear()

    # CONSTRAINT 3 (clean teardown / immutable state): the multi-target inject is
    # wrapped in try/finally so if we CANNOT find/redden the Nth target -- or any
    # unexpected exception fires mid-loop -- the N-1 already-mutated targets are
    # reverted byte-identically (reusing _restore_target) BEFORE control leaves,
    # leaving NO dangling mutated state. The finally only rolls back when the set
    # was NOT accepted (manifest not written).
    try:
        for tgt in targets:
            result = _mutate_one_target_red(cfg, tgt)
            if result is None:
                _log(
                    "decomposable: target %s::%s could not be turned red; dropping "
                    "it (no half-state)." % (
                        tgt.dotted_name or tgt.target_file, tgt.function,
                    )
                )
                continue
            entry, _orig = result
            applied.append(entry)

        # Acceptance check.
        if require_exact and len(applied) < n:
            _log(
                "require_exact: only %d/%d targets turned red -- rolling back ALL "
                "(no half-injected tree)." % (len(applied), n)
            )
            return 5
        if not applied:
            _log("decomposable: NO target turned red -- nothing injected (tree clean).")
            return 5

        # ACCEPTED: write the durable manifest INSIDE the try so the finally's
        # revert only fires on the NON-accepted paths.
        manifest = {
            "schema_version": DECOMPOSABLE_SCHEMA_VERSION,
            "injector_version": INJECTOR_VERSION,
            "mode": "decomposable",
            "requested_n": n,
            "targets": applied,
            "injected_at_iso": cfg.now_iso,
            "seed": cfg.seed,
        }
        _write_manifest(cfg.repo_root, manifest)
        accepted = True
        # Autonomous Self-Warming Oracle (Component 2): serialize the isolation
        # graph for the accepted targets so the Oracle can warm its index
        # without JIT compute during the soak. Fail-soft -- never fails inject.
        _write_prewarm_payload(
            cfg.repo_root, [e["target_file_abs"] for e in applied],
        )
        _log(
            "INJECTED decomposable chaos: %d MUTUALLY-ISOLATED target(s) red in %d "
            "distinct file(s). Manifest (schema 2) written." % (
                len(applied), len({e["target_file_abs"] for e in applied}),
            )
        )
        print(json.dumps({
            "status": "injected_decomposable",
            "count": len(applied),
            "requested_n": n,
            "targets": [
                {
                    "target_file": e["target_file"],
                    "function": e["function"],
                    "line": e["line"],
                    "mutation_kind": e["mutation_kind"],
                    "test_node": e["test_node"],
                    "test_red_post": e["test_red_post"],
                }
                for e in applied
            ],
        }, indent=2))
        return 0
    finally:
        if not accepted and applied:
            # Reached on every NON-accepted exit (return 5, or a raised exception
            # propagating through the loop) -- guarantees the N-1 byte-identical
            # revert (CONSTRAINT 3). On the accepted path this is a no-op.
            _rollback_all()


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
    mode.add_argument("--inject-decomposable", action="store_true",
                      help="Acquire + mutate N MUTUALLY-ISOLATED pure-leaf functions "
                           "(N different files, import-disjoint via the AST graph) so "
                           "they fan out into N parallel L3 subagents. N-entry manifest, "
                           "revert-ALL byte-identical.")
    mode.add_argument("--revert", action="store_true",
                      help="Restore the original source from the active manifest.")
    mode.add_argument("--status", action="store_true",
                      help="Show the active chaos manifest (if any).")
    mode.add_argument("--dry-run", action="store_true",
                      help="Acquire + show candidate + planned mutation. ZERO writes.")
    mode.add_argument("--list-candidates", action="store_true",
                      help="List all viable pure-leaf + green-test targets found.")

    p.add_argument("-n", "--num-targets", type=int, default=3,
                   help="Number of mutually-isolated targets for --inject-decomposable.")
    p.add_argument("--require-exact", action="store_true",
                   help="--inject-decomposable: refuse (no mutation) unless exactly N "
                        "isolated targets are acquired AND turn red.")
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
    p.add_argument("--verbose", dest="verbose", action="store_true", default=True,
                   help="Emit hyper-observability [prevalidate]/[adaptive]/[yield] "
                        "trace lines (default ON).")
    p.add_argument("--quiet", dest="verbose", action="store_false",
                   help="Suppress the hyper-observability trace lines.")
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
        verbose=bool(getattr(args, "verbose", True)),
    )

    if args.inject:
        return do_inject(cfg)
    if args.inject_decomposable:
        return do_inject_decomposable(
            cfg, n=int(args.num_targets), require_exact=bool(args.require_exact),
        )
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

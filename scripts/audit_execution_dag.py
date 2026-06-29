#!/usr/bin/env python3
"""
audit_execution_dag.py -- Static execution-reachability engine for the JARVIS O+V codebase.

PURPOSE
-------
Build a best-effort STATIC call graph of the production Ouroboros tree, compute the set of
capabilities (classes / module-level functions) reachable from the live production entrypoint
(`GovernedLoopService` public methods), and classify every capability into one of three buckets:

  [BUCKET A] LIVE & WIRED  -- reachable by a directed call path from the entrypoint.
  [BUCKET B] THEATER       -- imported into a live-reachable module but NEVER invoked on the
                              live path (wired-but-inert). HIGH PRIORITY.
  [BUCKET C] GHOST         -- zero inbound imports from any live-reachable module (orphaned).

It also runs three "blindspot" sweeps over nodes on/near the reachable execution path:
  * Shadow Swallows   -- except handlers that swallow (no raise / no DLQ-escalate route).
  * Hardcoded Artifacts -- OS-bound paths / localhost+port / numeric timeout literals on the path.
  * Async Starvation  -- naked synchronous blocking calls inside `async def` bodies.

HARD CONSTRAINTS
----------------
* Pure stdlib only (ast / os / glob / json / argparse / re / sys / dataclasses).
* NEVER imports any runtime module -- it only `ast.parse`s source text. No side effects.
* Deterministic + re-runnable: all collections are sorted before emission.

HONESTY / CONFIDENCE
--------------------
Static analysis cannot see dynamic dispatch. The following patterns cause FALSE POSITIVES
(capability flagged inert when it is actually live) and FALSE NEGATIVES (capability flagged
live when it is dead):
  * getattr()/dispatch tables/registry+plugin patterns -- invocation target is a runtime string.
  * Lazy / function-local imports -- not always tied to module-top import records.
  * Duck-typed callbacks, asyncio.create_task(coro_factory), partial(), decorators-as-dispatch.
  * Name collisions: two classes/functions sharing a simple name are resolved by best-effort
    (same-class -> same-module -> any-module), which over-links and can mark a dead node live.
Edges resolved via "any-module fallback" are tagged low-confidence and counted separately.
Treat BUCKET B/C as LEADS to verify (grep the callers), not proof of death.
"""

from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

# --------------------------------------------------------------------------------------
# Config / constants
# --------------------------------------------------------------------------------------

DEFAULT_TREE = "backend/core/ouroboros"
ENTRY_CLASS = "GovernedLoopService"
ENTRY_FILE_HINT = "governed_loop_service.py"

# Backup / duplicate copies to skip (Finder/iCloud style and *_2/_3 suffixes).
SKIP_FILE_SUFFIXES = ("_2.py", "_3.py")
# Finder-style copies: "name 2.py", "name 4.py", "name copy.py", "name copy 3.py".
SKIP_NAME_RE = re.compile(r"( \d+| copy(?: \d+)?)\.py$")
SKIP_PATH_PARTS = ("__pycache__", os.sep + "tests" + os.sep, "/tests/")

BLOCKING_ATTR_CALLS = {
    # attr-tail -> human label
    "sleep": "time.sleep",
    "run": "subprocess.run/.run",
    "check_output": "subprocess.check_output",
    "check_call": "subprocess.check_call",
    "call": "subprocess.call",
    "get": "requests.get/blocking",
    "post": "requests.post/blocking",
    "put": "requests.put/blocking",
    "delete": "requests.delete/blocking",
    "commit": ".commit() sync DB/git",
    "execute": ".execute() sync DB",
}
ASYNC_OFFLOADERS = {"to_thread", "run_in_executor"}

ESCALATE_TOKENS = ("dlq", "quarantine", "escalate", "reraise", "re_raise")

HARDCODED_PATH_TOKENS = ("/tmp", "/opt", "/var/", "/private/tmp", "\\\\", ".\\")
LOCALHOST_TOKENS = ("localhost", "127.0.0.1", "0.0.0.0")
TIMEOUT_KW_NAMES = ("timeout", "timeout_s", "timeout_sec", "deadline", "interval", "delay")


# --------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------

@dataclass
class Capability:
    """A top-level class or module-level function -- a 'tool/engine/helper' candidate."""
    qualname: str          # module::Name  (or module::Class.method for methods)
    simple_name: str
    kind: str              # "class" | "function" | "method" | "async_function" | "async_method"
    module: str            # dotted-ish module path relative to repo root (file based)
    file: str
    lineno: int
    parent_class: Optional[str] = None  # for methods
    calls: Set[str] = field(default_factory=set)  # simple names this node calls


@dataclass
class ModuleInfo:
    module: str
    file: str
    # alias -> target module simple ('import x as y' -> y: 'x'; 'import a.b' -> 'a.b': 'a.b')
    import_module_aliases: Dict[str, str] = field(default_factory=dict)
    # imported simple symbol names via 'from m import N' -> set of N
    imported_symbols: Set[str] = field(default_factory=set)
    # module sources referenced via 'from m import ...' -> set of m (raw)
    from_modules: Set[str] = field(default_factory=set)


# --------------------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------------------

def discover_files(root: str) -> List[str]:
    out: List[str] = []
    for path in glob.glob(os.path.join(root, "**", "*.py"), recursive=True):
        norm = path.replace("\\", "/")
        if any(part in norm for part in ("/__pycache__/",)):
            continue
        if "/tests/" in norm or os.path.basename(norm).startswith("test_"):
            continue
        base = os.path.basename(path)
        if any(base.endswith(suf) for suf in SKIP_FILE_SUFFIXES):
            continue
        if SKIP_NAME_RE.search(base):
            continue
        out.append(path)
    return sorted(set(out))


def module_name_for(path: str, repo_root: str) -> str:
    rel = os.path.relpath(path, repo_root).replace("\\", "/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    return rel.replace("/", ".")


# --------------------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------------------

def attach_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node  # type: ignore[attr-defined]


def call_target_name(call: ast.Call) -> Optional[str]:
    """Best-effort: resolve the simple callee name from an ast.Call."""
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def collect_calls(fn: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            n = call_target_name(node)
            if n:
                names.add(n)
    return names


def is_pure_pass_or_log(handler: ast.ExceptHandler) -> Tuple[bool, str]:
    """Return (is_swallow, reason) for an except handler body."""
    body = handler.body
    # Any raise anywhere -> not a swallow.
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return (False, "")
        if isinstance(node, ast.Call):
            tgt = (call_target_name(node) or "").lower()
            if any(tok in tgt for tok in ESCALATE_TOKENS):
                return (False, "")
            # logger.error with a re-route? still suspicious but error-level is less of a swallow
    # Now decide swallow shape.
    only_pass = all(isinstance(s, ast.Pass) for s in body)
    if only_pass:
        return (True, "bare pass")
    # only logging at warning/debug level + optional pass/return None
    suspicious = True
    reason = "warn/debug-log-only or None-fallback swallow"
    found_signal = False
    for s in body:
        if isinstance(s, ast.Pass):
            continue
        if isinstance(s, ast.Return):
            # return None / return mock-ish -> fallback swallow
            if s.value is None or (isinstance(s.value, ast.Constant) and s.value.value is None):
                found_signal = True
                continue
            # returning a value (fallback object) -- still a swallow candidate
            found_signal = True
            reason = "returns fallback value (possible mock/None) swallow"
            continue
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
            tgt = (call_target_name(s.value) or "").lower()
            if tgt in ("warning", "debug", "warn", "info"):
                found_signal = True
                continue
            # other call -> probably doing real work; not a pure swallow
            suspicious = False
            break
        # assignment / control flow -> real handling
        suspicious = False
        break
    if suspicious and found_signal:
        return (True, reason)
    return (False, "")


# --------------------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------------------

class Indexer:
    def __init__(self, repo_root: str, tree_root: str) -> None:
        self.repo_root = repo_root
        self.tree_root = tree_root
        self.capabilities: Dict[str, Capability] = {}     # qualname -> Capability
        self.modules: Dict[str, ModuleInfo] = {}          # module -> ModuleInfo
        self.simple_to_qualnames: Dict[str, Set[str]] = {}  # simple name -> qualnames
        self.parse_errors: List[str] = []
        # blindspot raw hits
        self.shadow_swallows: List[Dict] = []
        self.hardcoded: List[Dict] = []
        self.async_starvation: List[Dict] = []
        # per-file ast cache keyed by module
        self._trees: Dict[str, ast.AST] = {}
        self._module_file: Dict[str, str] = {}

    def index_file(self, path: str) -> None:
        module = module_name_for(path, self.repo_root)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src, filename=path)
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            self.parse_errors.append(f"{path}: {type(exc).__name__}: {exc}")
            return
        attach_parents(tree)
        self._trees[module] = tree
        self._module_file[module] = path
        minfo = ModuleInfo(module=module, file=path)
        self.modules[module] = minfo

        self._index_imports(tree, minfo)
        self._index_defs(tree, module, path)

    def _index_imports(self, tree: ast.AST, minfo: ModuleInfo) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    asname = alias.asname or alias.name.split(".")[0]
                    minfo.import_module_aliases[asname] = alias.name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                minfo.from_modules.add(mod)
                for alias in node.names:
                    name = alias.asname or alias.name
                    if alias.name != "*":
                        minfo.imported_symbols.add(name)
                        # also track the original symbol name for resolution
                        minfo.imported_symbols.add(alias.name)

    def _register(self, cap: Capability) -> None:
        self.capabilities[cap.qualname] = cap
        self.simple_to_qualnames.setdefault(cap.simple_name, set()).add(cap.qualname)

    def _index_defs(self, tree: ast.Module, module: str, path: str) -> None:
        for node in tree.body:  # top-level only for capability granularity
            if isinstance(node, ast.ClassDef):
                cls_qual = f"{module}::{node.name}"
                cap = Capability(
                    qualname=cls_qual, simple_name=node.name, kind="class",
                    module=module, file=path, lineno=node.lineno,
                    calls=collect_calls(node),  # class-body level calls (decorators, base init)
                )
                self._register(cap)
                # methods
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        kind = "async_method" if isinstance(sub, ast.AsyncFunctionDef) else "method"
                        mq = f"{module}::{node.name}.{sub.name}"
                        mcap = Capability(
                            qualname=mq, simple_name=sub.name, kind=kind,
                            module=module, file=path, lineno=sub.lineno,
                            parent_class=node.name, calls=collect_calls(sub),
                        )
                        self._register(mcap)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                fq = f"{module}::{node.name}"
                cap = Capability(
                    qualname=fq, simple_name=node.name, kind=kind,
                    module=module, file=path, lineno=node.lineno,
                    calls=collect_calls(node),
                )
                self._register(cap)


# --------------------------------------------------------------------------------------
# Graph construction + reachability
# --------------------------------------------------------------------------------------

class Graph:
    def __init__(self, idx: Indexer) -> None:
        self.idx = idx
        self.edges: Dict[str, Set[str]] = {}        # caller qual -> set(callee qual)
        self.low_conf_edges: Set[Tuple[str, str]] = set()
        self._build()

    def _resolve_callee(self, caller: Capability, simple: str) -> Tuple[Set[str], bool]:
        """Return (candidate qualnames, low_confidence)."""
        cands = self.idx.simple_to_qualnames.get(simple)
        if not cands:
            return (set(), False)
        # 1. same class
        if caller.parent_class:
            same_cls = f"{caller.module}::{caller.parent_class}.{simple}"
            if same_cls in cands:
                return ({same_cls}, False)
        # 2. same module (function or method)
        same_mod = {q for q in cands if q.split("::", 1)[0] == caller.module}
        if same_mod:
            return (same_mod, False)
        # 3. imported symbol resolution: if caller module imports `simple`, link to defs of it
        minfo = self.idx.modules.get(caller.module)
        if minfo and simple in minfo.imported_symbols:
            # link to top-level defs (class/function) named simple anywhere
            top = {q for q in cands if "." not in q.split("::", 1)[1]}
            if top:
                return (top, len(top) > 1)
        # 4. any-module fallback (low confidence) -- prefer top-level defs
        top = {q for q in cands if "." not in q.split("::", 1)[1]}
        chosen = top or cands
        return (chosen, True)

    def _build(self) -> None:
        for qual, cap in self.idx.capabilities.items():
            outs: Set[str] = set()
            for simple in cap.calls:
                cands, low = self._resolve_callee(cap, simple)
                for c in cands:
                    if c == qual:
                        continue
                    outs.add(c)
                    if low:
                        self.low_conf_edges.add((qual, c))
            # A class is considered to 'call' its own methods implicitly when instantiated
            self.edges[qual] = outs

    def entrypoints(self) -> List[str]:
        eps: List[str] = []
        for qual, cap in self.idx.capabilities.items():
            if cap.parent_class == ENTRY_CLASS and not cap.simple_name.startswith("_"):
                eps.append(qual)
            # also the class itself + the module-level submit helpers
            if cap.simple_name == ENTRY_CLASS and cap.kind == "class":
                eps.append(qual)
        return sorted(set(eps))

    def reachable(self, entrypoints: List[str], max_depth: Optional[int]) -> Set[str]:
        seen: Set[str] = set()
        frontier: List[Tuple[str, int]] = [(e, 0) for e in entrypoints]
        # Seed: a class entry pulls in its methods so their bodies are explored.
        for e in entrypoints:
            seen.add(e)
        while frontier:
            node, depth = frontier.pop()
            if max_depth is not None and depth >= max_depth:
                continue
            for callee in self.edges.get(node, ()):  # type: ignore[arg-type]
                if callee not in seen:
                    seen.add(callee)
                    frontier.append((callee, depth + 1))
                # If we reach a class, also expand its methods (instantiation -> method use).
                cap = self.idx.capabilities.get(callee)
                if cap and cap.kind == "class":
                    for q, c in self.idx.capabilities.items():
                        if c.parent_class == cap.simple_name and c.module == cap.module:
                            if q not in seen:
                                seen.add(q)
                                frontier.append((q, depth + 1))
        return seen


# --------------------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------------------

def classify(idx: Indexer, _graph: Graph, reachable: Set[str]) -> Dict[str, List[Dict]]:
    reachable_modules = {q.split("::", 1)[0] for q in reachable}

    # Build: which modules import a given simple symbol (live importers).
    live_importers_of: Dict[str, Set[str]] = {}
    for mod, minfo in idx.modules.items():
        for sym in minfo.imported_symbols:
            live_importers_of.setdefault(sym, set()).add(mod)

    bucket_a: List[Dict] = []
    bucket_b: List[Dict] = []
    bucket_c: List[Dict] = []

    # Capability granularity for buckets: top-level classes + module-level functions.
    for qual, cap in idx.capabilities.items():
        if cap.kind in ("method", "async_method"):
            continue  # methods classified via their class
        rec = {
            "qualname": qual,
            "name": cap.simple_name,
            "kind": cap.kind,
            "module": cap.module,
            "file": cap.file,
            "lineno": cap.lineno,
        }

        # Is this capability live? (itself reachable, or -- for classes -- any method reachable)
        is_live = qual in reachable
        if cap.kind == "class" and not is_live:
            prefix = f"{cap.module}::{cap.simple_name}."
            is_live = any(r.startswith(prefix) for r in reachable)

        if is_live:
            bucket_a.append(rec)
            continue

        # Not live. Imported by a live (reachable) module?
        importers = live_importers_of.get(cap.simple_name, set())
        live_importers = sorted(importers & reachable_modules)
        any_importers = sorted(importers)
        if live_importers:
            rec["imported_by_live_modules"] = live_importers[:10]
            bucket_b.append(rec)
        elif any_importers:
            # imported somewhere, but not by any live module -> still ghost-from-live-graph
            rec["imported_by_dead_modules"] = any_importers[:10]
            bucket_c.append(rec)
        else:
            rec["imported_by_live_modules"] = []
            bucket_c.append(rec)

    bucket_a.sort(key=lambda r: r["qualname"])
    bucket_b.sort(key=lambda r: r["qualname"])
    bucket_c.sort(key=lambda r: r["qualname"])
    return {"A": bucket_a, "B": bucket_b, "C": bucket_c}


# --------------------------------------------------------------------------------------
# Blindspot sweeps (only over reachable modules == on/near live path)
# --------------------------------------------------------------------------------------

def in_async_def(node: ast.AST) -> bool:
    cur = getattr(node, "parent", None)
    while cur is not None:
        if isinstance(cur, ast.AsyncFunctionDef):
            return True
        if isinstance(cur, (ast.FunctionDef,)):
            return False  # nearest enclosing func is sync
        cur = getattr(cur, "parent", None)
    return False


def is_offloaded(call: ast.Call) -> bool:
    """Is this blocking call wrapped in to_thread/run_in_executor (await-offloaded)?"""
    cur = getattr(call, "parent", None)
    hops = 0
    while cur is not None and hops < 6:
        if isinstance(cur, ast.Call):
            tgt = call_target_name(cur)
            if tgt in ASYNC_OFFLOADERS:
                return True
        if isinstance(cur, ast.Await):
            # await foo(...) where the awaited is an offloader handled above
            pass
        cur = getattr(cur, "parent", None)
        hops += 1
    return False


def sweep_blindspots(idx: Indexer, reachable_modules: Set[str]) -> None:
    for module in sorted(reachable_modules):
        tree = idx._trees.get(module)
        path = idx._module_file.get(module, module)
        if tree is None:
            continue
        for node in ast.walk(tree):
            # Shadow swallows
            if isinstance(node, ast.ExceptHandler):
                swallow, reason = is_pure_pass_or_log(node)
                if swallow:
                    etype = "bare"
                    if node.type is not None:
                        try:
                            etype = ast.unparse(node.type)
                        except Exception:
                            etype = "<expr>"
                    idx.shadow_swallows.append({
                        "file": path, "lineno": node.lineno,
                        "exc_type": etype, "reason": reason,
                    })

            # Hardcoded artifacts -- string literals
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value
                lo = val.lower()
                hit = None
                if any(tok in val for tok in HARDCODED_PATH_TOKENS):
                    hit = "os_path"
                elif any(tok in lo for tok in LOCALHOST_TOKENS):
                    hit = "localhost"
                if hit:
                    idx.hardcoded.append({
                        "file": path, "lineno": getattr(node, "lineno", -1),
                        "kind": hit, "value": val[:80],
                    })

            # Hardcoded numeric timeout literals passed as obvious timeout kwargs
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg and kw.arg.lower() in TIMEOUT_KW_NAMES:
                        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, (int, float)):
                            idx.hardcoded.append({
                                "file": path, "lineno": getattr(node, "lineno", -1),
                                "kind": "timeout_literal",
                                "value": f"{kw.arg}={kw.value.value}",
                            })

            # Async starvation -- blocking calls inside async def, not offloaded
            if isinstance(node, ast.Call) and in_async_def(node):
                if is_offloaded(node):
                    continue
                label = None
                f = node.func
                if isinstance(f, ast.Attribute):
                    tail = f.attr
                    base = ""
                    if isinstance(f.value, ast.Name):
                        base = f.value.id
                    if tail == "sleep" and base in ("time",):
                        label = "time.sleep"
                    elif base == "subprocess" and tail in ("run", "check_output", "check_call", "call", "Popen"):
                        label = f"subprocess.{tail}"
                    elif base == "requests" and tail in ("get", "post", "put", "delete", "patch", "head"):
                        label = f"requests.{tail}"
                    elif tail in ("commit", "execute") and base not in ("self",):
                        # crude DB/git sync op (skip self.execute which is often the loop's own)
                        label = f".{tail}() sync"
                elif isinstance(f, ast.Name):
                    if f.id == "open":
                        label = "open() blocking io"
                if label:
                    idx.async_starvation.append({
                        "file": path, "lineno": getattr(node, "lineno", -1),
                        "call": label,
                    })


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

def dedup(rows: List[Dict]) -> List[Dict]:
    seen = set()
    out = []
    for r in rows:
        key = json.dumps(r, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def build_report(repo_root, tree_root, idx, graph, reachable, buckets, entrypoints):
    reachable_modules = sorted({q.split("::", 1)[0] for q in reachable})
    shadow = dedup(idx.shadow_swallows)
    hard = dedup(idx.hardcoded)
    starv = dedup(idx.async_starvation)
    shadow.sort(key=lambda r: (r["file"], r["lineno"]))
    hard.sort(key=lambda r: (r["file"], r["lineno"]))
    starv.sort(key=lambda r: (r["file"], r["lineno"]))

    total_caps = sum(1 for c in idx.capabilities.values()
                     if c.kind not in ("method", "async_method"))
    report = {
        "_header": {
            "tool": "audit_execution_dag.py",
            "analysis_type": "STATIC ast.parse call-graph reachability (NO runtime import)",
            "repo_root": repo_root,
            "tree_root": tree_root,
            "entry_class": ENTRY_CLASS,
            "entrypoint_count": len(entrypoints),
            "confidence_caveats": [
                "Dynamic dispatch (getattr/registry/plugin) is invisible -> false 'theater'/'ghost'.",
                "Lazy/function-local imports may not be tied to module-top import records.",
                "Name-collision resolution over-links (any-module fallback) -> may mark dead nodes live.",
                "asyncio.create_task / partial / decorator-dispatch edges not traced.",
                "BUCKET B/C are LEADS to verify by grepping callers, not proof of death.",
            ],
            "low_confidence_edge_count": len(graph.low_conf_edges),
            "parse_errors": idx.parse_errors,
        },
        "stats": {
            "files_parsed": len(idx.modules),
            "capabilities_total": total_caps,
            "reachable_nodes": len(reachable),
            "reachable_modules": len(reachable_modules),
            "bucket_A_live": len(buckets["A"]),
            "bucket_B_theater": len(buckets["B"]),
            "bucket_C_ghost": len(buckets["C"]),
            "shadow_swallows": len(shadow),
            "hardcoded_artifacts": len(hard),
            "async_starvation": len(starv),
            "total_call_edges": sum(len(v) for v in graph.edges.values()),
        },
        "entrypoints": entrypoints,
        "buckets": {
            "A_live_wired": buckets["A"],
            "B_theater_wired_but_inert": buckets["B"],
            "C_ghosts_orphaned": buckets["C"],
        },
        "blindspots": {
            "shadow_swallows": shadow,
            "hardcoded_artifacts": hard,
            "async_starvation": starv,
        },
    }
    return report


def print_summary(report: Dict) -> None:
    s = report["stats"]
    h = report["_header"]
    line = "=" * 78
    print(line)
    print(" EXECUTION-REACHABILITY AUDIT  (static ast.parse -- no runtime import)")
    print(line)
    print(f" repo_root        : {h['repo_root']}")
    print(f" entry class      : {h['entry_class']}  ({h['entrypoint_count']} public entrypoints)")
    print(f" files parsed     : {s['files_parsed']}")
    print(f" capabilities     : {s['capabilities_total']} (top-level classes + module fns)")
    print(f" call edges       : {s['total_call_edges']}  (low-conf: {h['low_confidence_edge_count']})")
    print(f" reachable nodes  : {s['reachable_nodes']}  across {s['reachable_modules']} modules")
    print(line)
    print(f" [A] LIVE & WIRED : {s['bucket_A_live']}")
    print(f" [B] THEATER      : {s['bucket_B_theater']}   <-- wired-but-inert (HIGH PRIORITY)")
    print(f" [C] GHOSTS       : {s['bucket_C_ghost']}   <-- orphaned from live graph")
    print(line)
    print(" BLINDSPOTS (on/near live path):")
    print(f"   shadow swallows    : {s['shadow_swallows']}")
    print(f"   hardcoded artifacts: {s['hardcoded_artifacts']}")
    print(f"   async starvation   : {s['async_starvation']}")
    print(line)

    def head(title, rows, n=25):
        print(f"\n{title}  (showing {min(n, len(rows))}/{len(rows)})")
        for r in rows[:n]:
            extra = ""
            if "imported_by_live_modules" in r and r["imported_by_live_modules"]:
                extra = f"  <- imported by {len(r['imported_by_live_modules'])} live mod(s)"
            print(f"   {r['kind']:16} {r['name']:32} {r['module']}:{r['lineno']}{extra}")

    head("[BUCKET B] THEATER (full)", report["buckets"]["B_theater_wired_but_inert"], n=10**6)
    head("[BUCKET C] GHOSTS (top by name)", report["buckets"]["C_ghosts_orphaned"], n=40)

    def blind(title, rows, fmt, n=15):
        print(f"\n{title}  (showing {min(n, len(rows))}/{len(rows)})")
        for r in rows[:n]:
            print("   " + fmt(r))

    blind("SHADOW SWALLOWS", report["blindspots"]["shadow_swallows"],
          lambda r: f"{r['file']}:{r['lineno']}  except {r['exc_type']} -> {r['reason']}")
    blind("HARDCODED ARTIFACTS", report["blindspots"]["hardcoded_artifacts"],
          lambda r: f"{r['file']}:{r['lineno']}  [{r['kind']}] {r['value']}")
    blind("ASYNC STARVATION", report["blindspots"]["async_starvation"],
          lambda r: f"{r['file']}:{r['lineno']}  {r['call']}")
    print()


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Static execution-reachability audit for JARVIS O+V.")
    ap.add_argument("--repo-root", default=os.getcwd(),
                    help="Repo root (default: cwd).")
    ap.add_argument("--tree-root", default=None,
                    help=f"Production tree to analyze (default: <repo>/{DEFAULT_TREE}).")
    ap.add_argument("--json-out", default=None,
                    help="Path to write JSON report (default: stdout summary only).")
    ap.add_argument("--max-depth", type=int, default=None,
                    help="Max BFS depth from entrypoints (default: unbounded).")
    args = ap.parse_args(argv)

    repo_root = os.path.abspath(args.repo_root)
    tree_root = args.tree_root or os.path.join(repo_root, DEFAULT_TREE)
    tree_root = os.path.abspath(tree_root)

    if not os.path.isdir(tree_root):
        print(f"ERROR: tree root not found: {tree_root}", file=sys.stderr)
        return 2

    files = discover_files(tree_root)
    idx = Indexer(repo_root, tree_root)
    for f in files:
        idx.index_file(f)

    graph = Graph(idx)
    entrypoints = graph.entrypoints()
    if not entrypoints:
        print(f"WARNING: no entrypoints found for class {ENTRY_CLASS}", file=sys.stderr)
    reachable = graph.reachable(entrypoints, args.max_depth)
    reachable_modules = {q.split("::", 1)[0] for q in reachable}
    sweep_blindspots(idx, reachable_modules)
    buckets = classify(idx, graph, reachable)

    report = build_report(repo_root, tree_root, idx, graph, reachable, buckets, entrypoints)

    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=False)
        print(f"[written] JSON report -> {args.json_out}")

    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

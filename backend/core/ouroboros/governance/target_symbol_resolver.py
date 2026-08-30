"""TargetSymbolResolver — the keystone that turns an op into precise repair targets.

The interceptor/swarm (#70020-#70027) needs the CONCRETE functions an op intends
to repair. The pipeline carries ``target_files`` (paths) but no function-level
targets, so routing big files to the swarm without this would degrade to
RAG-only and regress the working bounded-diff path. This resolver closes that
gap with a **deterministic-first cascade** — hallucination is the root cause of
extraction drift, so probability is the LAST resort, never the first:

  1. **Stack-trace mapping (100% deterministic).** Parse the failing test's
     traceback frames → ``(file, line, func)`` and map each in-file line to its
     enclosing AST node. Zero guessing. Confidence 1.0.
  2. **Goal keyword match (deterministic heuristic, NO LLM).** No trace? Score
     each symbol name by token overlap with the ``goal`` text (an explicit
     whole-name mention scores 1.0). Still no model call — no hallucination.
  3. **Fail-Closed.** Best confidence below the floor → return EMPTY. The caller
     skips the interceptor entirely and the standard generation route is
     byte-identical. Ambiguity NEVER routes to the swarm.

Two structural guarantees on every resolved target:

  * **Call-Graph Expansion (Symbol Clustering).** A resolved function that calls
    LOCAL sibling/helper methods in the same file pulls them into the target
    cluster — the swarm sees the primary target AND its local dependencies, so a
    node is never repaired context-starved.
  * **Decorator-Aware Anchoring.** A symbol's span starts at its HIGHEST-level
    decorator (``@classmethod`` / ``@property`` / ``@retry`` ...) and runs
    through its docstring + body, so the swarm can never orphan a wrapper. This
    matches ``ASTChunker``'s convention (``include_decorators=True``), so the
    downstream ``extract_target_chunk`` agrees byte-for-byte.

DRY: native ``ast`` for the index; composes ``PlanGenerator._extract_symbols``
for the top-level name universe and the attribution bridge's ``traceback_frames``
/ ``source_loci``. Pure + deterministic (no I/O, sub-ms) — async is unnecessary
and would be fake; the caller invokes it inline before dispatch.
"""

from __future__ import annotations

import ast
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("Ouroboros.TargetSymbolResolver")

# `File "path/to/x.py", line 42, in func` — the CPython traceback frame shape.
_TB_FRAME_RE = re.compile(
    r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>[^\s,]+)'
)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")

_MIN_CONF_ENV = "JARVIS_SYMBOL_RESOLVER_MIN_CONFIDENCE"
_DEFAULT_MIN_CONF = 0.5
_CLUSTER_ENV = "JARVIS_SYMBOL_RESOLVER_CLUSTER_ENABLED"
_MAX_CLUSTER_ENV = "JARVIS_SYMBOL_RESOLVER_MAX_CLUSTER"
_DEFAULT_MAX_CLUSTER = 6
_MAX_PRIMARY_ENV = "JARVIS_SYMBOL_RESOLVER_MAX_PRIMARY"
_DEFAULT_MAX_PRIMARY = 4

METHOD_DECLARED = "declared_symbol"
METHOD_STACK_TRACE = "stack_trace"
METHOD_GOAL_KEYWORD = "goal_keyword"
METHOD_UNRESOLVED = "unresolved"


def _min_confidence() -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get(_MIN_CONF_ENV, str(_DEFAULT_MIN_CONF)))))
    except (TypeError, ValueError):
        return _DEFAULT_MIN_CONF


def _cluster_enabled() -> bool:
    return os.environ.get(_CLUSTER_ENV, "true").strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedSymbol:
    """One repair target with a decorator-inclusive AST anchor."""
    name: str                     # qualified: "Class.method" or bare "func"
    start_line: int               # 1-indexed, INCLUDES the highest decorator
    end_line: int                 # 1-indexed inclusive (docstring + body)
    kind: str                     # "function" | "async_function" | "method" | "async_method"
    decorators: Tuple[str, ...] = ()
    is_cluster_member: bool = False   # pulled in by call-graph expansion


@dataclass(frozen=True)
class ResolutionResult:
    symbols: Tuple[ResolvedSymbol, ...] = ()
    method: str = METHOD_UNRESOLVED
    confidence: float = 0.0
    primary: Tuple[str, ...] = ()       # names resolved as primary targets
    cluster: Tuple[str, ...] = ()       # names added by call-graph expansion

    @property
    def resolved(self) -> bool:
        return bool(self.symbols)

    @property
    def symbol_names(self) -> Tuple[str, ...]:
        return tuple(s.name for s in self.symbols)


# ---------------------------------------------------------------------------
# AST index — spans (decorator-aware), kinds, local call edges
# ---------------------------------------------------------------------------


@dataclass
class _SymDef:
    name: str
    basename: str
    start_line: int      # decorator-inclusive
    def_line: int        # the def/class line itself
    end_line: int
    kind: str
    decorators: Tuple[str, ...]
    calls: Set[str]      # bare callee names invoked in the body


def _anchor_start(node: ast.AST) -> int:
    """Decorator-aware anchor — the highest-level decorator's line, else the
    def line. Guarantees a wrapped symbol is never orphaned at the stitch."""
    decos = getattr(node, "decorator_list", None) or []
    if decos:
        return min(int(d.lineno) for d in decos)
    return int(getattr(node, "lineno", 1))


def _collect_calls(node: ast.AST) -> Set[str]:
    """Local callee names invoked inside *node* — ``f(...)`` → ``f``,
    ``self.helper(...)`` / ``obj.helper(...)`` → ``helper``. Intersected with the
    file's own symbols later, so only LOCAL siblings survive."""
    names: Set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Name):
                names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.add(fn.attr)
    return names


def _mk_symdef(node: ast.AST, cls: Optional[str]) -> _SymDef:
    base = node.name  # type: ignore[attr-defined]
    qual = f"{cls}.{base}" if cls else base
    is_async = isinstance(node, ast.AsyncFunctionDef)
    if cls:
        kind = "async_method" if is_async else "method"
    else:
        kind = "async_function" if is_async else "function"
    return _SymDef(
        name=qual,
        basename=base,
        start_line=_anchor_start(node),
        def_line=int(getattr(node, "lineno", 1)),
        end_line=int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
        kind=kind,
        decorators=tuple(_safe_unparse(d) for d in (getattr(node, "decorator_list", None) or [])),
        calls=_collect_calls(node),
    )


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return getattr(node, "id", "") or getattr(node, "attr", "") or "?"


def _index(source: str) -> List[_SymDef]:
    """Flat index of top-level functions + one level of class methods. Nested
    functions are intentionally excluded — the swarm repairs top-level defs and
    methods, not closures. Never raises."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: List[_SymDef] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(_mk_symdef(node, None))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(_mk_symdef(sub, node.name))
    return out


def _plan_extract_symbols(source: str) -> Set[str]:
    """DRY corroboration via the existing ``PlanGenerator._extract_symbols`` —
    the top-level name universe. Lazy + fail-soft (empty set on any import/parse
    trouble) so the resolver never hard-depends on the plan module."""
    try:
        from backend.core.ouroboros.governance.plan_generator import PlanGenerator
        raw = PlanGenerator._extract_symbols(source)  # ["class X", "def y", ...]
    except Exception:  # noqa: BLE001
        return set()
    names: Set[str] = set()
    for entry in raw or ():
        parts = str(entry).split()
        if parts:
            names.add(parts[-1])
    return names


# ---------------------------------------------------------------------------
# Cascade helpers
# ---------------------------------------------------------------------------


def _parse_frames(frames: Sequence[str]) -> List[Tuple[str, int, str]]:
    """Extract ``(file, line, func)`` triples from raw traceback frame strings.
    A frame may itself be a multi-line block; scan every match."""
    out: List[Tuple[str, int, str]] = []
    for raw in frames or ():
        for m in _TB_FRAME_RE.finditer(str(raw)):
            try:
                out.append((m.group("file"), int(m.group("line")), m.group("func")))
            except (TypeError, ValueError):
                continue
    return out


def _file_matches(frame_file: str, target: str) -> bool:
    if not frame_file or not target:
        return False
    fb, tb = os.path.basename(frame_file), os.path.basename(target)
    if fb != tb:
        return False
    return frame_file.endswith(target) or target.endswith(frame_file) or fb == tb


def _enclosing(index: List[_SymDef], line: int) -> Optional[_SymDef]:
    """The innermost symbol whose decorator-inclusive span contains *line*.
    Innermost = smallest span (a method inside a class wins over nothing)."""
    best: Optional[_SymDef] = None
    for sym in index:
        if sym.start_line <= line <= sym.end_line:
            if best is None or (sym.end_line - sym.start_line) < (best.end_line - best.start_line):
                best = sym
    return best


def _subtokens(name: str) -> Set[str]:
    """Split an identifier into lowercased sub-tokens across ``_`` and camelCase,
    plus the whole lowercased name. ``_topological_sort`` →
    {topological, sort, _topological_sort}; ``buildGraph`` → {build, graph}."""
    toks: Set[str] = set()
    low = name.lower().strip()
    if low:
        toks.add(low.lstrip("_"))
    for part in name.split("_"):
        for cm in _CAMEL_RE.findall(part):
            if cm:
                toks.add(cm.lower())
    toks.discard("")
    return toks


def _goal_tokens(goal: str) -> Set[str]:
    toks: Set[str] = set()
    low = goal.lower()
    for m in _IDENT_RE.finditer(low):
        toks.add(m.group(0))
        for part in m.group(0).split("_"):
            if part:
                toks.add(part)
    return toks


def _name_score(sym: _SymDef, goal_low: str, goal_toks: Set[str]) -> float:
    """Deterministic goal-affinity score in [0, 1]. Explicit whole-name mention →
    1.0; else the fraction of the symbol's sub-tokens present in the goal."""
    base = sym.basename.lower()
    # Explicit mention of the exact function name is unambiguous.
    if re.search(r"\b" + re.escape(base) + r"\b", goal_low):
        return 1.0
    sub = _subtokens(sym.basename)
    if not sub:
        return 0.0
    hits = sum(1 for t in sub if t in goal_toks)
    return hits / len(sub)


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def resolve_target_symbols(
    *,
    source: str,
    file_path: str,
    traceback_frames: Sequence[str] = (),
    source_loci: Sequence[str] = (),
    goal: str = "",
    declared_symbols: Sequence[str] = (),
    min_confidence: Optional[float] = None,
    expand_cluster: Optional[bool] = None,
) -> ResolutionResult:
    """Resolve the concrete repair targets for one file via the deterministic
    cascade. Never raises. An empty result means FAIL-CLOSED — the caller must
    skip the interceptor and take the standard generation route unchanged."""
    floor = _min_confidence() if min_confidence is None else max(0.0, min(1.0, min_confidence))
    do_cluster = _cluster_enabled() if expand_cluster is None else bool(expand_cluster)

    index = _index(source)
    if not index:
        return ResolutionResult()  # unparseable / no symbols → fail-closed

    by_base: Dict[str, _SymDef] = {}
    for sym in index:
        # First definition of a basename wins as the cluster/trace anchor.
        by_base.setdefault(sym.basename, sym)
    top_level_universe = _plan_extract_symbols(source)  # DRY corroboration
    if top_level_universe:
        logger.debug(
            "[TargetSymbolResolver] %s: %d top-level names corroborated via "
            "PlanGenerator._extract_symbols", file_path, len(top_level_universe),
        )

    primaries: List[_SymDef] = []
    method = METHOD_UNRESOLVED
    confidence = 0.0
    loci = {os.path.basename(p) for p in source_loci or ()}

    # ── Pass 0: operator-DECLARED symbols ──────────────────────────────────
    # Ordered ahead of every inference pass because it is not an inference.
    # A stack trace is evidence, a goal keyword is a guess, and a declared
    # symbol is an INSTRUCTION — carried on a goal whose HMAC the risk engine
    # has already verified before the op was allowed to exist at all.
    #
    # Measured cost of not having this (soak bt-2026-08-28-115654): a goal
    # declaring `_should_use_lean_prompt` resolved instead to
    # `_read_with_truncation` at confidence 0.50, because the only signal that
    # reached here was prose and the prose said "truncation" more often than it
    # said the symbol's name. The declaration existed; nothing read it.
    #
    # Confidence 1.0, matching METHOD_STACK_TRACE: both are facts rather than
    # scores, so neither should be filterable by a confidence floor an operator
    # raised to suppress weak keyword guesses.
    #
    # Names are matched against the file's OWN symbol index, so a declaration
    # naming something absent from this file resolves nothing here and the
    # inference passes still run — a typo degrades to the old behaviour instead
    # of fabricating a target.
    declared = tuple(
        str(s).strip() for s in (declared_symbols or ()) if str(s).strip()
    )
    if declared:
        _wanted = {d for d in declared}
        for sym in index:
            if sym.name in _wanted or sym.basename in _wanted:
                if sym.name not in {p.name for p in primaries}:
                    primaries.append(sym)
        if primaries:
            method = METHOD_DECLARED
            confidence = 1.0
            logger.info(
                "[TargetSymbolResolver] %s: DECLARED target(s) honoured %s "
                "(operator-signed; inference passes skipped)",
                file_path, [p.name for p in primaries],
            )
        else:
            logger.info(
                "[TargetSymbolResolver] %s: declared symbol(s) %s not present "
                "in this file — falling back to inference",
                file_path, list(declared),
            )

    # ── Pass 1: deterministic stack-trace mapping ──
    # Guarded on `not primaries` for the same reason Pass 2 already is: a
    # declared target must not be widened by frames it did not ask for, and
    # `if primaries:` below would otherwise relabel a DECLARED result as
    # STACK_TRACE even with zero frames parsed — a resolution reporting a
    # provenance it does not have.
    if not primaries:
        for frame_file, line, func in _parse_frames(traceback_frames):
            in_this_file = _file_matches(frame_file, file_path) or (
                os.path.basename(frame_file) in loci and os.path.basename(frame_file) == os.path.basename(file_path)
            )
            if not in_this_file:
                continue
            hit = _enclosing(index, line)
            if hit is None and func in by_base:      # line drifted → func-name tie-break
                hit = by_base[func]
            if hit is not None and hit.name not in {p.name for p in primaries}:
                primaries.append(hit)
        if primaries:
            method = METHOD_STACK_TRACE
            confidence = 1.0

    # ── Pass 2: deterministic goal keyword match (NO LLM) ──
    if not primaries and goal.strip():
        goal_low = goal.lower()
        goal_toks = _goal_tokens(goal)
        scored = sorted(
            ((sym, _name_score(sym, goal_low, goal_toks)) for sym in index),
            key=lambda t: t[1], reverse=True,
        )
        best = scored[0][1] if scored else 0.0
        picked = [sym for sym, sc in scored if sc >= floor]
        if picked and best >= floor:
            primaries = picked[: _int_env(_MAX_PRIMARY_ENV, _DEFAULT_MAX_PRIMARY)]
            method = METHOD_GOAL_KEYWORD
            confidence = best

    # ── Fail-Closed ──
    if not primaries:
        logger.info(
            "[TargetSymbolResolver] %s: FAIL-CLOSED (no confident target — "
            "trace=%d goal=%r) → standard route preserved",
            file_path, len(_parse_frames(traceback_frames)), goal[:60],
        )
        return ResolutionResult(method=METHOD_UNRESOLVED, confidence=0.0)

    primary_names = [p.name for p in primaries]

    # ── Call-Graph Expansion: pull local siblings/helpers into the cluster ──
    cluster: List[_SymDef] = []
    if do_cluster:
        max_cluster = _int_env(_MAX_CLUSTER_ENV, _DEFAULT_MAX_CLUSTER)
        seen = set(primary_names)
        for p in primaries:
            for callee in sorted(p.calls):
                if len(cluster) >= max_cluster:
                    break
                dep = by_base.get(callee)
                if dep is not None and dep.name not in seen:
                    seen.add(dep.name)
                    cluster.append(dep)

    symbols: List[ResolvedSymbol] = []
    for p in primaries:
        symbols.append(_to_public(p, is_cluster=False))
    for c in cluster:
        symbols.append(_to_public(c, is_cluster=True))

    logger.info(
        "[TargetSymbolResolver] %s: %s (conf=%.2f) primary=%s cluster=%s",
        file_path, method, confidence, primary_names, [c.name for c in cluster],
    )
    return ResolutionResult(
        symbols=tuple(symbols),
        method=method,
        confidence=confidence,
        primary=tuple(primary_names),
        cluster=tuple(c.name for c in cluster),
    )


def _to_public(sym: _SymDef, *, is_cluster: bool) -> ResolvedSymbol:
    return ResolvedSymbol(
        name=sym.name,
        start_line=sym.start_line,
        end_line=sym.end_line,
        kind=sym.kind,
        decorators=sym.decorators,
        is_cluster_member=is_cluster,
    )


__all__ = [
    "METHOD_GOAL_KEYWORD",
    "METHOD_DECLARED",
    "METHOD_STACK_TRACE",
    "METHOD_UNRESOLVED",
    "ResolutionResult",
    "ResolvedSymbol",
    "resolve_target_symbols",
]
